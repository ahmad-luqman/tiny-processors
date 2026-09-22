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
    reg [31:0] program_mem [0:255];
    reg [15:0] data_mem [0:255];
    reg [7:0] entry;
    wire busy, done, fault;
    wire [31:0] cycles, stalls, transfers, instructions;
    wire [7:0] program_address, memory_address;
    wire memory_valid, memory_write;
    wire [15:0] memory_wdata;
    wire program_sel = (addr & 32'hffff_fc00) == 32'h2000_5000;
    wire data_sel = (addr & 32'hffff_fc00) == 32'h2000_6000;
    wire regs_sel = (addr & 32'hffff_ffe0) == 32'h2000_4000;
    wire word_ok = strb == 4'b1111 && addr[1:0] == 2'b00;
    wire [31:0] cpu_program = program_mem[addr[9:2]];
    wire [15:0] cpu_data = data_mem[addr[9:2]];
    reg access_ok;
    always @* begin
        access_ok = 1'b0;
        rdata = 32'd0;
        if (program_sel) begin
            access_ok = !busy;
            rdata = cpu_program;
        end else if (data_sel) begin
            access_ok = !busy;
            rdata = {16'd0, cpu_data};
        end else if (regs_sel) begin
            case (addr[4:2])
                3'd0: access_ok = we && ((wdata == 32'd1 && !busy) || wdata == 32'd2);
                3'd1: begin access_ok = !we; rdata = {29'd0, fault, done, busy}; end
                3'd2: begin access_ok = !we || (!busy && wdata < 32'd256); rdata = {24'd0, entry}; end
                3'd3: begin access_ok = !we; rdata = cycles; end
                3'd4: begin access_ok = !we; rdata = stalls; end
                3'd5: begin access_ok = !we; rdata = transfers; end
                3'd6: begin access_ok = !we; rdata = instructions; end
                default: begin end
            endcase
        end
    end
    assign ready = valid && !reset;
    assign error = !word_ok || !access_ok;
    wire accepted_write = valid && ready && !error && we;
    wire command = accepted_write && regs_sel && addr[4:2] == 3'd0;
    wire engine_reset = reset || (command && wdata == 32'd2);
    wire start = command && wdata == 32'd1;
    wire memory_ready = !memory_hold && !engine_reset;

    always @(posedge clk) begin
        if (engine_reset) entry <= 8'd0;
        else if (accepted_write && regs_sel && addr[4:2] == 3'd2) entry <= wdata[7:0];
        if (accepted_write && program_sel) program_mem[addr[9:2]] <= wdata;
        if (accepted_write && data_sel) data_mem[addr[9:2]] <= wdata[15:0];
        else if (memory_valid && memory_ready && memory_write)
            data_mem[memory_address] <= memory_wdata;
    end

    // Observability stays on named wires for waveforms; the CPU sees counters only.
    wire [7:0] pc, retire_pc;
    wire [31:0] instruction, retire_instruction;
    wire [1:0] state, memory_lane;
    wire [15:0] loop_count;
    wire retired;
    wire [255:0] register_state;
    wire [127:0] accumulator_state;
    simd4 engine (
        .clk(clk), .reset(engine_reset), .start(start), .entry_pc(entry),
        .busy(busy), .done(done), .fault(fault),
        .program_address(program_address), .program_data(program_mem[program_address]),
        .mem_valid(memory_valid), .mem_write(memory_write), .mem_address(memory_address),
        .mem_write_data(memory_wdata), .mem_ready(memory_ready), .mem_read_data(data_mem[memory_address]),
        .pc(pc), .instruction(instruction), .state(state), .memory_lane(memory_lane), .loop_count(loop_count),
        .retired(retired), .retire_pc(retire_pc), .retire_instruction(retire_instruction),
        .cycles(cycles), .stalls(stalls), .memory_transfers(transfers), .instructions(instructions),
        .register_state(register_state), .accumulator_state(accumulator_state)
    );
    wire unused_ok = &{1'b0, pc, instruction, state, memory_lane, loop_count,
                      retired, retire_pc, retire_instruction, register_state, accumulator_state};
endmodule
