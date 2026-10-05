`timescale 1ns/1ps

// virtio-blk (docs/rv32.md, "virtio-blk"; Track 2, O3): a block device in
// virtio-mmio version 2's register layout at QEMU virt's first virtio slot,
// with one split virtqueue and a disk held in DISK_WORDS words of memory
// (the testbench loads and saves it). The disk in the drive may be smaller:
// `disk_sectors` is its size, the capacity the device reports and checks
// requests against (issue #35), clamped to the memory. Word access only:
//
//   +0x000 MagicValue "virt"   +0x004 Version 2      +0x008 DeviceID 2 (block)
//   +0x00c VendorID            +0x010 DeviceFeatures (sel 1: bit 0, VERSION_1)
//   +0x014 DeviceFeaturesSel   +0x020 DriverFeatures +0x024 DriverFeaturesSel
//   +0x030 QueueSel            +0x034 QueueNumMax (8 for queue 0)
//   +0x038 QueueNum            +0x044 QueueReady     +0x050 QueueNotify
//   +0x060 InterruptStatus     +0x064 InterruptACK   +0x070 Status (0 resets)
//   +0x080/84 QueueDesc, +0x090/94 QueueDriver, +0x0a0/a4 QueueDevice (low/high)
//   +0x0fc ConfigGeneration 0  +0x100/104 capacity in 512-byte sectors
//
// A write of 0 to QueueNotify, with the queue ready and DRIVER_OK set, runs
// every request the driver has made available before the store is accepted:
// the device holds `ready` low (the bus contract lets any device wait) and
// meanwhile drives `dma_*`, which the machine connects to the CPU's side of
// the RAM port, since the CPU is stalled on this store. A request is a chain
// of three descriptors: a 16-byte header (type 0 IN or 1 OUT, and the
// sector), a data buffer of whole words, and a status byte. The device copies
// the data, writes the status (0 OK, 1 IOERR for a range past the disk or a
// misaligned buffer, 2 UNSUPP for another type), fills a used-ring element,
// advances the used index and sets InterruptStatus bit 0, which is `irq`. A
// chain it cannot follow (a descriptor index at or past QueueNum included), or a
// DMA address outside RAM, sets Status bit 6
// (DEVICE_NEEDS_RESET) and stops. tools/rv32emu_core.c runs the same steps.
module rv32_virtio_blk #(
    parameter integer DISK_WORDS = 2097152, // 8 MiB: 16,384 sectors, the largest disk
    parameter integer RAM_WORDS = 4194304,
    parameter [31:0] RAM_BASE = 32'h8000_0000
) (
    input  wire        clk,
    input  wire        reset,
    input  wire [31:0] disk_sectors,  // the size of the disk in the drive
    // Register port.
    input  wire        valid,
    input  wire        we,
    input  wire [31:0] addr,
    input  wire [3:0]  strb,
    input  wire [31:0] wdata,
    output wire [31:0] rdata,
    output wire        ready,
    output wire        error,
    // DMA into RAM while `busy`.
    output wire        busy,
    output reg         dma_valid,
    output reg         dma_we,
    output reg  [31:0] dma_addr,
    output reg  [3:0]  dma_strb,
    output reg  [31:0] dma_wdata,
    input  wire [31:0] dma_rdata,
    input  wire        dma_ready,
    output wire        irq
);
    localparam [31:0] MAGIC = 32'h7472_6976, VENDOR = 32'h594e_4954; // "virt", "TINY"
    localparam [31:0] QUEUE_MAX = 32'd8;
    localparam [31:0] RAM_BYTES = RAM_WORDS * 4;
    localparam [31:0] MAX_SECTORS = DISK_WORDS / 128;
    wire [31:0] sectors = disk_sectors > MAX_SECTORS ? MAX_SECTORS : disk_sectors;
`ifndef SYNTHESIS
    // The clamp keeps a synthesized device safe; in simulation a disk larger than the memory, or a
    // memory that is not whole sectors, is the testbench's mistake and stops the run.
    initial if (DISK_WORDS >= 128 && DISK_WORDS % 128 != 0)
        begin $display("rv32_virtio_blk: DISK_WORDS %0d is not whole 128-word sectors", DISK_WORDS); $stop; end
    always @(posedge clk)
        if (disk_sectors > MAX_SECTORS)
            begin $display("rv32_virtio_blk: a disk of %0d sectors does not fit the %0d-sector memory", disk_sectors, MAX_SECTORS); $stop; end
`endif
    localparam [3:0] IDLE = 4'd0, AVAIL_IDX = 4'd1, RING = 4'd2, DESC = 4'd3, HEADER = 4'd4, COPY = 4'd5,
                     STATUS = 4'd6, USED_ID = 4'd7, USED_LEN = 4'd8, USED_IDX = 4'd9, NEXT = 4'd10, FAIL = 4'd11;

    reg [31:0] disk [0:DISK_WORDS-1];

    reg [7:0] status;
    reg features_sel, queue_sel_zero, queue_ready, served; // the driver's features are accepted and not kept
    reg [3:0] queue_num;
    reg [31:0] desc_lo, desc_hi, driver_lo, driver_hi, device_lo, device_hi;
    reg [15:0] last_avail, used_idx;
    reg interrupt_status;

    // The request being served.
    reg [3:0] state;
    reg [1:0] which;        // descriptor 0 (header), 1 (data), 2 (status)
    reg [1:0] word;         // word of a descriptor or of the header being read
    reg [15:0] head, next;
    reg [31:0] header_addr, data_addr, data_len, status_addr, type_word, sector, sector_hi;
    reg data_write;         // descriptor 1's WRITE flag: the device writes the buffer
    reg [31:0] copied;      // words copied so far
    reg [7:0] result;

    wire [11:0] offset = addr[11:0];
    wire is_word = strb == 4'b1111;
    wire at_notify = offset == 12'h050;
    wire notify_starts = valid && we && is_word && at_notify && wdata == 32'd0 && queue_ready && status[2] &&
                         !status[6] && state == IDLE && !served;

    assign busy = state != IDLE;
    assign irq = interrupt_status;

    // Register reads.
    reg known;
    reg [31:0] value;
    always @* begin
        known = 1'b1;
        value = 32'd0;
        case (offset)
            12'h000: value = MAGIC;
            12'h004: value = 32'd2;
            12'h008: value = 32'd2;
            12'h00c: value = VENDOR;
            12'h010: value = {31'd0, features_sel};  // sel 1: VIRTIO_F_VERSION_1 (feature bit 32)
            12'h034: value = queue_sel_zero ? QUEUE_MAX : 32'd0;
            12'h044: value = {31'd0, queue_ready};
            12'h060: value = {31'd0, interrupt_status};
            12'h070: value = {24'd0, status};
            12'h080: value = desc_lo;
            12'h084: value = desc_hi;
            12'h090: value = driver_lo;
            12'h094: value = driver_hi;
            12'h0a0: value = device_lo;
            12'h0a4: value = device_hi;
            12'h0fc: value = 32'd0;
            12'h100: value = sectors;
            12'h104: value = 32'd0;
            12'h014, 12'h020, 12'h024, 12'h030, 12'h038, 12'h050, 12'h064: known = we; // write-only
            default: known = 1'b0;
        endcase
        if (we && (offset == 12'h000 || offset == 12'h004 || offset == 12'h008 || offset == 12'h00c ||
                   offset == 12'h010 || offset == 12'h034 || offset == 12'h060 || offset == 12'h0fc ||
                   offset == 12'h100 || offset == 12'h104))
            known = 1'b0; // read-only
    end

    assign rdata = value;
    assign error = !(known && is_word);
    // The notify that starts the work waits until it is served; every other access answers at once.
    wire hold = at_notify && we && is_word && !served && (notify_starts || busy);
    assign ready = valid && !hold;

    // DMA helpers: every address must lie in RAM.
    function in_ram;
        input [31:0] a;
        begin
            in_ram = a >= RAM_BASE && (a - RAM_BASE) < RAM_BYTES;
        end
    endfunction
    wire [15:0] half = dma_addr[1] ? dma_rdata[31:16] : dma_rdata[15:0];
    wire [2:0] queue_mask = queue_num[2:0] - 3'd1;    // queue sizes are powers of two, at most 8
    wire [31:0] avail_slot = driver_lo + 32'd4 + {28'd0, (last_avail[2:0] & queue_mask), 1'b0};
    wire [31:0] used_slot = device_lo + 32'd4 + {26'd0, (used_idx[2:0] & queue_mask), 3'd0};
    wire [31:0] desc_base = desc_lo + {12'd0, (which == 2'd0 ? head : next), 4'd0};
    wire [31:0] disk_index = sector * 32'd128 + copied;
    wire [38:0] disk_limit = {sectors, 7'd0};
    wire range_ok = sector_hi == 32'd0 && sector < sectors && data_len[1:0] == 2'd0 && data_addr[1:0] == 2'd0 &&
                    ({sector, 7'd0} + {9'd0, data_len[31:2]}) <= disk_limit;

    // Start a DMA access (and the state that consumes its answer).
    task access;
        input w;
        input [31:0] a;
        input [3:0] s;
        input [31:0] d;
        begin
            if (!in_ram(a)) begin
                state <= FAIL;
            end else begin
                dma_valid <= 1'b1;
                dma_we <= w;
                dma_addr <= a;
                dma_strb <= s;
                dma_wdata <= d;
            end
        end
    endtask

    always @(posedge clk) begin
        if (reset) begin
            status <= 8'd0;
            features_sel <= 1'b0;
            queue_sel_zero <= 1'b1;
            queue_ready <= 1'b0;
            queue_num <= 4'd0;
            desc_lo <= 32'd0; desc_hi <= 32'd0; driver_lo <= 32'd0; driver_hi <= 32'd0;
            device_lo <= 32'd0; device_hi <= 32'd0;
            last_avail <= 16'd0;
            used_idx <= 16'd0;
            interrupt_status <= 1'b0;
            served <= 1'b0;
            state <= IDLE;
            dma_valid <= 1'b0;
            dma_we <= 1'b0;
            dma_addr <= 32'd0;
            dma_strb <= 4'd0;
            dma_wdata <= 32'd0;
        end else begin
            // Register writes (never while serving: the CPU is waiting on the notify).
            if (valid && we && is_word && !error && !busy && !at_notify) begin
                case (offset)
                    12'h014: features_sel <= wdata == 32'd1;
                    12'h030: queue_sel_zero <= wdata == 32'd0;
                    12'h038: if (queue_sel_zero) queue_num <= (wdata == 32'd1 || wdata == 32'd2 || wdata == 32'd4 || wdata == 32'd8) ? wdata[3:0] : 4'd0;
                    12'h044: if (queue_sel_zero) queue_ready <= wdata[0];
                    12'h064: if (wdata[0]) interrupt_status <= 1'b0;
                    12'h070: begin
                        status <= wdata[7:0];
                        if (wdata[7:0] == 8'd0) begin // a device reset: the queue starts again
                            queue_ready <= 1'b0;
                            queue_num <= 4'd0;
                            last_avail <= 16'd0;
                            used_idx <= 16'd0;
                            interrupt_status <= 1'b0;
                            features_sel <= 1'b0;
                        end
                    end
                    12'h080: if (queue_sel_zero) desc_lo <= wdata;
                    12'h084: if (queue_sel_zero) desc_hi <= wdata;
                    12'h090: if (queue_sel_zero) driver_lo <= wdata;
                    12'h094: if (queue_sel_zero) driver_hi <= wdata;
                    12'h0a0: if (queue_sel_zero) device_lo <= wdata;
                    12'h0a4: if (queue_sel_zero) device_hi <= wdata;
                    default: begin end
                endcase
            end
            if (valid && ready && at_notify && served)
                served <= 1'b0;

            if (dma_valid) begin
                if (dma_ready) dma_valid <= 1'b0;
            end
            case (state)
                IDLE: if (notify_starts) begin
                    if (desc_hi != 32'd0 || driver_hi != 32'd0 || device_hi != 32'd0 || queue_num == 4'd0) begin
                        state <= FAIL;
                    end else begin
                        state <= AVAIL_IDX; // a halfword: `half` picks it by bit 1, the RAM ignores bits 1:0
                        access(1'b0, driver_lo + 32'd2, 4'b1111, 32'd0);
                    end
                end
                AVAIL_IDX: if (dma_valid && dma_ready) begin
                    if (half == last_avail) begin
                        state <= IDLE;
                        served <= 1'b1;
                    end else begin
                        state <= RING;
                        access(1'b0, avail_slot, 4'b1111, 32'd0);
                    end
                end
                RING: if (dma_valid && dma_ready) begin
                    if (half >= {12'd0, queue_num}) begin // the head names no descriptor of the queue
                        state <= FAIL;
                    end else begin
                        head <= half;
                        which <= 2'd0;
                        word <= 2'd0;
                        state <= DESC;
                        access(1'b0, desc_lo + {12'd0, half, 4'd0}, 4'b1111, 32'd0);
                    end
                end
                // Each descriptor is four words: address low, address high (must be 0), length,
                // and flags | next << 16. NEXT (bit 0) must be set on the first two and clear on
                // the last; WRITE (bit 1) must be clear on the header and set on the status.
                DESC: if (dma_valid && dma_ready) begin
                    if (word == 2'd0) begin
                        if (which == 2'd0) header_addr <= dma_rdata;
                        else if (which == 2'd1) data_addr <= dma_rdata;
                        else status_addr <= dma_rdata;
                    end
                    if (which == 2'd1 && word == 2'd2) data_len <= dma_rdata;
                    if (word == 2'd1 && dma_rdata != 32'd0) begin
                        state <= FAIL;
                    end else if (word != 2'd3) begin
                        word <= word + 2'd1;
                        access(1'b0, desc_base + {28'd0, word + 2'd1, 2'b00}, 4'b1111, 32'd0);
                    end else if ((which != 2'd2) != dma_rdata[0] || (which == 2'd0 && dma_rdata[1]) ||
                                 (which == 2'd2 && !dma_rdata[1]) ||
                                 (which != 2'd2 && dma_rdata[31:16] >= {12'd0, queue_num})) begin
                        state <= FAIL;
                    end else if (which == 2'd2) begin
                        word <= 2'd0;
                        state <= HEADER;
                        access(1'b0, header_addr, 4'b1111, 32'd0);
                    end else begin
                        if (which == 2'd1) data_write <= dma_rdata[1];
                        next <= dma_rdata[31:16];
                        which <= which + 2'd1;
                        word <= 2'd0;
                        access(1'b0, desc_lo + {12'd0, dma_rdata[31:16], 4'd0}, 4'b1111, 32'd0);
                    end
                end
                // The header: type at +0, sector at +8 and +12.
                HEADER: if (dma_valid && dma_ready) begin
                    if (word == 2'd0) begin
                        type_word <= dma_rdata;
                        word <= 2'd2;
                        access(1'b0, header_addr + 32'd8, 4'b1111, 32'd0);
                    end else if (word == 2'd2) begin
                        sector <= dma_rdata;
                        word <= 2'd3;
                        access(1'b0, header_addr + 32'd12, 4'b1111, 32'd0);
                    end else begin
                        sector_hi <= dma_rdata;
                        copied <= 32'd0;
                        state <= COPY;
                    end
                end
                COPY: begin
                    if (type_word > 32'd1) begin
                        result <= 8'd2; // UNSUPP
                        state <= STATUS;
                    end else if (!range_ok || data_write != (type_word == 32'd0)) begin
                        result <= 8'd1; // IOERR
                        state <= STATUS;
                    end else if (copied == {2'd0, data_len[31:2]}) begin
                        result <= 8'd0;
                        state <= STATUS;
                    end else if (!dma_valid) begin
                        if (type_word == 32'd0)   // IN: disk to RAM
                            access(1'b1, data_addr + {copied[29:0], 2'b00}, 4'b1111, disk[disk_index]);
                        else                      // OUT: RAM to disk
                            access(1'b0, data_addr + {copied[29:0], 2'b00}, 4'b1111, 32'd0);
                    end else if (dma_ready) begin
                        if (type_word == 32'd1) disk[disk_index] <= dma_rdata;
                        copied <= copied + 32'd1;
                    end
                end
                STATUS: if (!dma_valid) begin
                    access(1'b1, status_addr, 4'b0001 << status_addr[1:0], {4{result}});
                end else if (dma_ready) begin
                    state <= USED_ID;
                    access(1'b1, used_slot, 4'b1111, {16'd0, head});
                end
                USED_ID: if (dma_valid && dma_ready) begin
                    state <= USED_LEN;
                    // Bytes written into device-writable buffers: the data of an IN, and the status.
                    access(1'b1, used_slot + 32'd4, 4'b1111,
                           (result == 8'd0 && type_word == 32'd0) ? data_len + 32'd1 : 32'd1);
                end
                USED_LEN: if (dma_valid && dma_ready) begin
                    state <= USED_IDX;
                    access(1'b1, device_lo + 32'd2, device_lo[1] ? 4'b0011 : 4'b1100,
                           {2{used_idx + 16'd1}});
                end
                USED_IDX: if (dma_valid && dma_ready) begin
                    used_idx <= used_idx + 16'd1;
                    last_avail <= last_avail + 16'd1;
                    interrupt_status <= 1'b1;
                    state <= NEXT;
                end
                NEXT: begin
                    state <= AVAIL_IDX;
                    access(1'b0, driver_lo + 32'd2, 4'b1111, 32'd0);
                end
                FAIL: begin
                    status <= status | 8'h40; // DEVICE_NEEDS_RESET
                    dma_valid <= 1'b0;
                    state <= IDLE;
                    served <= 1'b1;
                end
                default: state <= FAIL;
            endcase
        end
    end

    wire unused_ok = &{1'b0, addr[31:12], disk_index};
endmodule
