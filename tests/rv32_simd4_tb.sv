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
    integer fd, count, rows = 0, expected_rows, i, field_index, field_count, bits;
    reg [407:0] fields [0:17];
    reg expected_ready;
    reg expected_error;
    reg [31:0] expected_data, status, entry, cycles, stalls, transfers, instructions;
    reg [407:0] snapshot;
    reg [25:0] transfer;
    reg held = 0;
    reg [24:0] held_request;
    wire [24:0] request = {dut.memory_write, dut.memory_address, dut.memory_wdata};
    string fixture, wave;
    // Scan text before narrowing. %h directly into a small destination can
    // silently truncate malformed fixture values on either simulator.
    task automatic read_field;
        input integer width;
        output reg [407:0] value;
        output integer consumed;
        string token;
        integer index, character;
        reg [3:0] digit;
        begin
            value = 408'd0;
            consumed = $fscanf(fd, "%s", token);
            if (consumed == 0 && $feof(fd)) consumed = -1;
            if (consumed == 1) begin
                if (token.len() == 0 || token.len() > (width+3)/4)
                    $fatal(1, "Fixture field width at row %0d", rows);
                for (index = 0; index < token.len(); index = index+1) begin
                    character = {24'd0, token[index]};
                    if (character >= 48 && character <= 57) digit = character[3:0];
                    else if ((character >= 65 && character <= 70) || (character >= 97 && character <= 102))
                        digit = character[3:0] + 4'd9;
                    else $fatal(1, "Invalid fixture hex at row %0d", rows);
                    value = (value << 4) | {404'd0, digit};
                end
                if ((value >> width) != 408'd0)
                    $fatal(1, "Fixture field width at row %0d", rows);
            end
        end
    endtask
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
        count = 1;
        while (count == 1) begin
            read_field(1, fields[0], count);
            if (count == 1) begin
                for (field_index = 1; field_index < 18; field_index = field_index+1) begin
                    case (field_index)
                        1, 2, 6, 7, 17: bits = 1;
                        4: bits = 4;
                        15: bits = 408;
                        16: bits = 26;
                        default: bits = 32;
                    endcase
                    read_field(bits, fields[field_index], field_count);
                    if (field_count != 1) $fatal(1, "Incomplete fixture at row %0d", rows);
                end
                reset = fields[0][0]; valid = fields[1][0]; we = fields[2][0];
                addr = fields[3][31:0]; strb = fields[4][3:0]; wdata = fields[5][31:0];
                memory_hold = fields[6][0]; expected_error = fields[7][0]; expected_data = fields[8][31:0];
                status = fields[9][31:0]; entry = fields[10][31:0]; cycles = fields[11][31:0];
                stalls = fields[12][31:0]; transfers = fields[13][31:0]; instructions = fields[14][31:0];
                snapshot = fields[15]; transfer = fields[16][25:0]; expected_ready = fields[17][0];
                #4;
                if ((^{reset, valid, we, addr, strb, wdata, memory_hold,
                    expected_error, expected_data, status, entry, cycles, stalls, transfers, instructions, snapshot, transfer}) === 1'bx)
                    $fatal(1, "Unknown fixture at row %0d", rows);
                if (ready !== expected_ready) $fatal(1, "Ready row %0d", rows);
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
