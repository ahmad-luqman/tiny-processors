`timescale 1ns/1ps

// Bus-facing ownership and storage for the unchanged A1 engine.
// docs/rv32-simd4.md specifies every accepted access and reset edge.
module rv32_simd4 (
    input wire clk, reset,
    input wire valid, we,
    input wire [31:0] addr, wdata,
    input wire [3:0] strb,
    output reg [31:0] rdata,
    output wire ready, error,
    input wire memory_hold
);
    localparam [31:0] SIMD4_BASE = 32'h2000_4000;
    localparam [31:0] SIMD4_PROGRAM = 32'h2000_5000;
    localparam [31:0] SIMD4_DATA = 32'h2000_6000;
    localparam [4:0] SIMD4_COMMAND = 5'h00;
    localparam [4:0] SIMD4_STATUS = 5'h04;
    localparam [4:0] SIMD4_ENTRY = 5'h08;
    localparam [4:0] SIMD4_CYCLES = 5'h0c;
    localparam [4:0] SIMD4_STALLS = 5'h10;
    localparam [4:0] SIMD4_TRANSFERS = 5'h14;
    localparam [4:0] SIMD4_INSTRUCTIONS = 5'h18;
    localparam [31:0] SIMD4_BUSY = 32'h00000001;
    localparam [31:0] SIMD4_DONE = 32'h00000002;
    localparam [31:0] SIMD4_FAULT = 32'h00000004;
    localparam [31:0] SIMD4_START = 32'h00000001;
    localparam [31:0] SIMD4_RESET = 32'h00000002;
    localparam integer LANES = 4;
    reg [31:0] program_mem [0:255];
    reg [15:0] data_mem [0:255];
    reg [7:0] entry;
    wire busy, done, fault;
    wire [31:0] cycles, stalls, transfers, instructions;
    wire [7:0] program_address, memory_address;
    wire memory_valid, memory_write;
    wire [15:0] memory_wdata;
    wire program_sel = (addr & 32'hffff_fc00) == SIMD4_PROGRAM;
    wire data_sel = (addr & 32'hffff_fc00) == SIMD4_DATA;
    wire regs_sel = (addr & 32'hffff_ffe0) == SIMD4_BASE;
    wire word_ok = strb == 4'b1111 && addr[1:0] == 2'b00;
    // Ownership selects an address BEFORE each array, inferring one read
    // port rather than a CPU port plus an engine port with a mux afterwards.
    wire [7:0] program_read_address = busy ? program_address : addr[9:2];
    wire [7:0] data_address = busy ? memory_address : addr[9:2];
    wire [31:0] program_read_data = program_mem[program_read_address];
    wire [15:0] data_read_data = data_mem[data_address];
    reg access_ok;
    always @* begin
        access_ok = 1'b0;
        rdata = 32'd0;
        if (program_sel) begin
            access_ok = !busy;
            rdata = program_read_data;
        end else if (data_sel) begin
            access_ok = !busy;
            rdata = {16'd0, data_read_data};
        end else if (regs_sel) begin
            case (addr[4:0])
                SIMD4_COMMAND: access_ok = we && ((wdata == SIMD4_START && !busy) || wdata == SIMD4_RESET);
                SIMD4_STATUS: begin access_ok = !we; rdata = ({32{busy}} & SIMD4_BUSY) | ({32{done}} & SIMD4_DONE) | ({32{fault}} & SIMD4_FAULT); end
                SIMD4_ENTRY: begin access_ok = !we || (!busy && wdata < 32'd256); rdata = {24'd0, entry}; end
                SIMD4_CYCLES: begin access_ok = !we; rdata = cycles; end
                SIMD4_STALLS: begin access_ok = !we; rdata = stalls; end
                SIMD4_TRANSFERS: begin access_ok = !we; rdata = transfers; end
                SIMD4_INSTRUCTIONS: begin access_ok = !we; rdata = instructions; end
                default: begin end
            endcase
        end
    end
    assign ready = valid && !reset;
    assign error = !word_ok || !access_ok;
    wire accepted_write = valid && ready && !error && we;
    wire command = accepted_write && regs_sel && addr[4:0] == SIMD4_COMMAND;
    wire engine_reset = reset || (command && wdata == SIMD4_RESET);
    wire start = command && wdata == SIMD4_START;
    wire memory_ready = !memory_hold && !engine_reset;

    wire data_write = (accepted_write && data_sel) || (memory_valid && memory_ready && memory_write);
    wire [15:0] data_write_value = busy ? memory_wdata : wdata[15:0];
    always @(posedge clk) begin
        if (engine_reset) entry <= 8'd0;
        else if (accepted_write && regs_sel && addr[4:0] == SIMD4_ENTRY) entry <= wdata[7:0];
        if (accepted_write && program_sel) program_mem[addr[9:2]] <= wdata;
        if (data_write) data_mem[data_address] <= data_write_value;
    end

    // Observability stays on named wires for waveforms; the CPU sees counters only.
    wire [7:0] pc, retire_pc;
    wire [31:0] instruction, retire_instruction;
    wire [1:0] state, memory_lane;
    wire [15:0] loop_count;
    wire retired;
    wire [LANES*64-1:0] register_state;
    wire [LANES*32-1:0] accumulator_state;
    simd4 #(.LANES(LANES)) engine (
        .clk(clk), .reset(engine_reset), .start(start), .entry_pc(entry),
        .busy(busy), .done(done), .fault(fault),
        .program_address(program_address), .program_data(program_read_data),
        .mem_valid(memory_valid), .mem_write(memory_write), .mem_address(memory_address),
        .mem_write_data(memory_wdata), .mem_ready(memory_ready), .mem_read_data(data_read_data),
        .pc(pc), .instruction(instruction), .state(state), .memory_lane(memory_lane), .loop_count(loop_count),
        .retired(retired), .retire_pc(retire_pc), .retire_instruction(retire_instruction),
        .cycles(cycles), .stalls(stalls), .memory_transfers(transfers), .instructions(instructions),
        .register_state(register_state), .accumulator_state(accumulator_state)
    );
    wire unused_ok = &{1'b0, pc, instruction, state, memory_lane, loop_count,
                      retired, retire_pc, retire_instruction, register_state, accumulator_state};
endmodule
