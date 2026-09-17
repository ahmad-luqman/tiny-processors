# Lab 01: a clocked counter

The circuit contains eight bits of state and logic to increment that state. `always @(posedge clk)` describes updates at a rising edge. It does not mean a software loop is running inside the FPGA.

`count <= count + 8'd1` schedules an update using the previous count. Nonblocking assignment (`<=`) is the usual choice for clocked state. `8'd1` is an explicitly eight-bit decimal value; an eight-bit result discards the carry beyond bit 7.

Reset is synchronous and active high. Asserting it between rising edges leaves the output unchanged until the next rising edge. The missing final `else` intentionally retains the flip-flop state when enable is low. A missing assignment in a combinational process has different implications: it can infer a latch.

## Read the trace

The testbench clock has a 10 ns period. It changes inputs on falling edges and checks outputs 1 ns after rising edges, allowing the nonblocking updates to settle. These delays belong to the testbench; the RTL has no delays.

| Time | Event | Expected count |
| --- | --- | --- |
| 10 ns | Reset and enable asserted on falling edge | Unchanged; initial state is unspecified |
| 15 ns | Rising edge samples reset | 0 |
| 20 ns | Reset and enable deasserted | 0 |
| 25 ns | Rising edge with enable low | 0 |
| 35 ns | First enabled rising edge after reset | 1 |
| 2575 ns | Last increment before reaching the limit | 255 |
| 2585 ns | Enable low | 255 |
| 2595 ns | Next enabled rising edge | 0 |

Icarus can show `x` before the first reset. Do not depend on a simulator's initial register value: reset establishes the defined starting state.

The tests check every value from 1 to 255, hold at zero/nonzero/maximum, wraparound, reset with enable high and low, and unchanged output between edges. They use explicit expected results and stop with a nonzero status on failure.

## Try next

1. Predict what happens if reset and enable are both high. Find that edge in the waveform.
2. Change the counter to saturate at 255. Run the tests and identify which expectation fails before updating the specification and test.
3. Add a direction input for increment/decrement. Define what happens when decrementing zero, then test it.

Keep the baseline working in Git before each experiment. The next main lab is an ALU; the counter's state-update pattern will return when we build the CPU's program counter.
