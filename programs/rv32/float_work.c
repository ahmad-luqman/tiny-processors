/* The same runtime-input kernel is compiled for RV32IF and RV32I software float.
 * No fast-math, contraction, or constant folding across this call boundary. */
__attribute__((noinline)) float float_work(float a, float b, float c)
{
    float product = a * b;
    return (product + c) / b - a;
}
