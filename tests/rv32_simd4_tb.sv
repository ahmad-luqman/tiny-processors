`timescale 1ns/1ps
// Replay one native-model fixture edge by edge, including MMIO faults,
// engine backpressure, reset, snapshots and all performance counters.
module rv32_simd4_tb;
    reg clk = 0, reset = 0, valid = 0, we = 0, memory_hold = 0;
    reg [31:0] addr = 0, wdata = 0;
    reg [3:0] strb = 0;
    wire [31:0] rdata;
    wire ready, error;
    rv32_simd4 dut (.*);
    integer fd, count, rows = 0, expected_rows, i;
    reg expected_error;
    reg [31:0] expected_data, status, entry, cycles, stalls, transfers, instructions;
    reg [407:0] snapshot;
    reg [25:0] transfer;
    reg held = 0;
    reg [24:0] held_request;
    wire [24:0] request = {dut.memory_write, dut.memory_address, dut.memory_wdata};
    string fixture, wave;
    initial begin
        if (!$value$plusargs("fixture=%s", fixture)) $fatal(1, "Missing fixture");
        if (!$value$plusargs("rows=%d", expected_rows) || expected_rows <= 0) $fatal(1, "Missing row count");
        if ($value$plusargs("wave=%s", wave)) begin
            fd = $fopen(wave, "w");
            if (fd == 0) $fatal(1, "Cannot write waveform");
            $fclose(fd);
            $dumpfile(wave);
            $dumpvars(0, rv32_simd4_tb);
        end
        for (i = 0; i < 256; i = i + 1) begin
            dut.program_mem[i] = 0;
            dut.data_mem[i] = 0;
        end
        fd = $fopen(fixture, "r");
        if (fd == 0) $fatal(1, "Cannot open fixture");
        count = 17;
        while (count == 17) begin
            count = $fscanf(fd, "%h %h %h %h %h %h %h %h %h %h %h %h %h %h %h %h %h\n",
                reset, valid, we, addr, strb, wdata, memory_hold, expected_error, expected_data,
                status, entry, cycles, stalls, transfers, instructions, snapshot, transfer);
            if (count == 17) begin
                #4;
                if ((^{reset, valid, we, addr, strb, wdata, memory_hold,
                    expected_error, expected_data, status, entry, cycles, stalls, transfers, instructions, snapshot, transfer}) === 1'bx)
                    $fatal(1, "Unknown fixture at row %0d", rows);
                if (ready !== (valid && !reset)) $fatal(1, "Ready row %0d", rows);
                if (valid && !reset) begin
                    if (error !== expected_error) $fatal(1, "Error row %0d addr %h", rows, addr);
                    if (!we && !expected_error && rdata !== expected_data)
                        $fatal(1, "Read row %0d addr %h got %h expected %h", rows, addr, rdata, expected_data);
                end
                if ((dut.memory_valid && dut.memory_ready) !== transfer[25])
                    $fatal(1, "Transfer acceptance row %0d", rows);
                if (transfer[25] && {dut.memory_write, dut.memory_address,
                    (dut.memory_write ? dut.memory_wdata : dut.data_read_data)} !== transfer[24:0])
                    $fatal(1, "Transfer contents row %0d", rows);
                if (dut.data_write !== ((transfer[25] && transfer[24]) ||
                    (valid && ready && !error && we && dut.data_sel)))
                    $fatal(1, "Memory write without acceptance row %0d", rows);
                if (held && !dut.engine_reset && (dut.memory_valid !== 1'b1 || request !== held_request))
                    $fatal(1, "Held request changed row %0d", rows);
                held = dut.memory_valid && !dut.memory_ready && !dut.engine_reset;
                held_request = request;
                clk = 1;
                #1;
                if ({29'd0, dut.fault, dut.done, dut.busy} !== status || {24'd0, dut.entry} !== entry ||
                    dut.cycles !== cycles || dut.stalls !== stalls || dut.transfers !== transfers ||
                    dut.instructions !== instructions)
                    $fatal(1, "State/counters row %0d cycles %0d/%0d", rows, dut.cycles, cycles);
                if ({dut.loop_count, dut.pc, dut.accumulator_state, dut.register_state} !== snapshot)
                    $fatal(1, "Snapshot row %0d", rows);
                #5 clk = 0;
                rows = rows + 1;
            end
        end
        if (count != -1 || rows != expected_rows) $fatal(1, "Incomplete fixture: %0d rows, %0d fields", rows, count);
        $fclose(fd);
        $display("PASS protocol %0d", rows);
    end
endmodule
