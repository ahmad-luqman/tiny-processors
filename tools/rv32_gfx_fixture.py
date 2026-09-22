"""Serialization of the byte-port corpus consumed by both native harnesses."""
GP_OP, GP_COLOR, GP_X0, GP_Y0, GP_X1, GP_Y1, GP_X2, GP_Y2, GP_W, GP_H, GP_SRC, GP_STRIDE, GP_SW, GP_SH, GP_SX, GP_SY = range(16)

def fixture_text(rows):
    return ''.join(f'{mode} {abort} '+' '.join(f'{v&0xffffffff:08x}' for v in params)+'\n'
                   for mode,abort,params in rows)
