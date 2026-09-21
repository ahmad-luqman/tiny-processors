// Module-local controller encodings, included by the RTL and protocol testbench.
// Keep RESPONSE last; the state-count pin test rejects gaps or trailing states.
localparam [3:0] IDLE=0, DECODE=1, ALIGN=2, COMBINE=3, NORMALIZE=4,
    DENORMALIZE=5, ROUND=6, DIVIDE=7, SQRT=8, INT_SHIFT=9,
    INT_ROUND=10, RESPONSE=11;
localparam integer STATE_COUNT = {28'b0,RESPONSE} + 32'd1;
