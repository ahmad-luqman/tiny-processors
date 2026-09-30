"""The DMA window (issue #20, docs/rv32.md "DMA window") on the emulator and the RTL.

The window bounds the RAM G1's blit source and G2's depth buffer may lie in; the engines test it when
they validate a job. Each program records STATUS and ERROR after every job in a results area, and
both backends must agree on the results and on every trap.
"""
import os
from pathlib import Path
import unittest

from tools.rv32_asm import (ADDI, ANDI, BNE, CSRRS, CSRRW, DMA_WINDOW, DMA_WINDOW_END, DMA_WINDOW_START, FB, FINISH,
                            G3D_BASE, G3D_CLEAR_Z, G3D_COMMAND, G3D_DONE, G3D_ERROR, G3D_FAULT, G3D_LIMIT, G3D_START,
                            G3D_STATUS, G3D_TCOUNT, G3D_VCOUNT, G3D_ZBASE, GPU_BASE, GPU_BLIT, GPU_COMMAND, GPU_DONE,
                            GPU_ERROR, GPU_FAULT, GPU_INVALID, GPU_PARAMS, GPU_START, GPU_STATUS, LB, LH, LI, LW, MRET,
                            RAM, RAM_SIZE, SB, SW)
from tools.rv32_rtl import check_passed, compare_backends, run_emulator, run_rtl, write_image

RESULTS = RAM + 0x3000          # where the programs store what they read
HANDLER = RAM + 0x1000          # skips a faulting instruction
Z_BYTES = 320 * 240 * 2
G3D_E_PARAM = 1


def handler(words):
    """Pad to the handler and append one that steps mepc past the faulting instruction."""
    assert len(words) * 4 <= HANDLER - RAM, len(words)
    return words + [0] * ((HANDLER - RAM) // 4 - len(words)) + [CSRRS(6, 0x341, 0), ADDI(6, 6, 4), CSRRW(0, 0x341, 6),
                                                                MRET()]


def wait(base, status):
    """Poll a STATUS register (x1 holds the device base) until BUSY clears."""
    return [LW(5, base, status), ANDI(5, 5, 1), BNE(5, 0, -8)]


def record(slot, base, offset):
    """Store the word at base+offset to RESULTS+4*slot (x9 holds RESULTS)."""
    return [LW(5, base, offset), SW(5, 9, 4 * slot)]


class DmaWindowTest(unittest.TestCase):
    def setUp(self):
        verilator = os.environ.get("G1_SIM") == "verilator"
        self.out = Path("build/rv32/dma-window-" + ("verilator" if verilator else "icarus"))
        self.out.mkdir(parents=True, exist_ok=True)
        self.sim = "build/verilator-rv32/rv32_sim" if verilator else "build/rv32/rv32_tb.vvp"

    def run_pair(self, words, name, **timing):
        hex_path, bin_path = write_image(handler(words), self.out, name)
        emulator = run_emulator("build/rv32/rv32emu", bin_path, self.out / f"{name}.emu.trace")
        rtl = run_rtl(self.sim, hex_path, self.out / f"{name}.rtl.trace", **timing)
        check_passed(emulator)
        check_passed(rtl)
        self.assertIsNone(compare_backends(rtl, emulator, "results"))
        return emulator, rtl

    def stored(self, run):
        """The words the program stored to RESULTS, by slot."""
        found = {}
        for line in run.trace:
            if "mem[" in line and "<-" in line:
                address, value = line.split("mem[", 1)[1].split("]<-", 1)
                address = int(address, 16)
                if RESULTS <= address < RESULTS + 0x100:
                    found[(address - RESULTS) // 4] = int(value.split("/")[0], 16)
        return found

    def traps(self, run):
        return [tuple(line.split()[-2:]) for line in run.trace if " trap " in line]

    def test_registers_reset_to_all_of_ram_and_refuse_narrow_accesses(self):
        words = LI(1, DMA_WINDOW) + LI(9, RESULTS) + LI(4, HANDLER) + [CSRRW(0, 0x305, 4)]
        words += record(0, 1, DMA_WINDOW_START) + record(1, 1, DMA_WINDOW_END)
        words += LI(4, RAM + 0x20000) + [SW(4, 1, DMA_WINDOW_START)] + LI(4, RAM + 0x30000) + [SW(4, 1, DMA_WINDOW_END)]
        words += record(2, 1, DMA_WINDOW_START) + record(3, 1, DMA_WINDOW_END)
        # Bytes, halfwords and the word past END fault; the registers keep their values.
        words += [LB(5, 1, 0), LH(5, 1, 4), SB(4, 1, 0), LW(5, 1, 8), SW(4, 1, 8)]
        words += record(4, 1, DMA_WINDOW_START) + FINISH()
        for run in self.run_pair(words, "registers"):
            self.assertEqual(self.stored(run), {0: RAM, 1: RAM + RAM_SIZE, 2: RAM + 0x20000, 3: RAM + 0x30000,
                                                4: RAM + 0x20000})
            window = f"{DMA_WINDOW:08x}"
            self.assertEqual(self.traps(run), [("5", window), ("5", f"{DMA_WINDOW + 4:08x}"), ("7", window),
                                               ("5", f"{DMA_WINDOW + 8:08x}"), ("7", f"{DMA_WINDOW + 8:08x}")])

    def blit(self, slot, source, width=4, rows=4):
        """A 4 by 4 blit to the frame's corner from a `width` by `rows` source at `source` (stride
        `width`); records STATUS and ERROR."""
        return self.start_blit(source, width, rows) + wait(1, GPU_STATUS) + \
            record(2 * slot, 1, GPU_STATUS) + record(2 * slot + 1, 1, GPU_ERROR)

    def start_blit(self, source, width=4, rows=4, size=4):
        p = [GPU_BLIT, 0, 0, 0, 0, 0, 0, 0, size, size, source, width, width, rows, 0, 0]
        words = []
        for i, value in enumerate(p):
            words += LI(4, value & 0xffffffff) + [SW(4, 1, GPU_PARAMS + 4 * i)]
        return words + LI(4, GPU_START) + [SW(4, 1, GPU_COMMAND)]

    def test_g1_source_must_lie_in_the_window(self):
        start, end = RAM + 0x20000, RAM + 0x20000 + 64
        words = LI(1, GPU_BASE) + LI(2, DMA_WINDOW) + LI(9, RESULTS)
        words += self.blit(0, RAM + 0x10000)                       # reset: all of RAM
        words += LI(4, start) + [SW(4, 2, DMA_WINDOW_START)] + LI(4, end) + [SW(4, 2, DMA_WINDOW_END)]
        words += self.blit(1, RAM + 0x10000)                       # below START
        words += self.blit(2, start)                               # 16 bytes at START
        words += self.blit(3, end - 16)                            # ends exactly at END
        words += self.blit(4, end - 12)                            # one row past END
        words += self.blit(5, start - 4)                           # starts one row before START
        words += self.blit(6, FB, width=320, rows=240)            # the framebuffer is not RAM: no window
        # A blit validated inside the window runs to the end even if the window then shrinks to
        # nothing, as G2's job does below.
        words += LI(4, start + 0x1000) + [SW(4, 2, DMA_WINDOW_END)]  # room for a 64 by 64 source
        words += self.start_blit(start, width=64, rows=64, size=64)
        words += [ADDI(0, 0, 0)] * 4 + [SW(0, 2, DMA_WINDOW_START), SW(0, 2, DMA_WINDOW_END)]
        words += wait(1, GPU_STATUS) + record(14, 1, GPU_STATUS) + record(15, 1, GPU_ERROR)
        words += FINISH()
        done, invalid = (GPU_DONE, 0), (GPU_FAULT, GPU_INVALID)
        expected = [done, invalid, done, done, invalid, invalid, done, done]
        for run in self.run_pair(words, "g1-window", stall=1, gpu_stall=2):
            stored = self.stored(run)
            self.assertEqual([(stored[2 * i], stored[2 * i + 1]) for i in range(8)], expected)
            self.assertEqual(self.traps(run), [])

    def clear(self, slot, zbase):
        words = LI(4, zbase) + [SW(4, 1, G3D_ZBASE)] + LI(4, G3D_CLEAR_Z) + [SW(4, 1, G3D_COMMAND)]
        return words + wait(1, G3D_STATUS) + record(2 * slot, 1, G3D_STATUS) + record(2 * slot + 1, 1, G3D_ERROR)

    def test_g2_depth_buffer_must_lie_in_the_window(self):
        start = RAM + 0x40000
        words = LI(1, G3D_BASE) + LI(2, DMA_WINDOW) + LI(9, RESULTS)
        words += self.clear(0, RAM + 0x100000)                     # reset: all of RAM
        words += LI(4, start) + [SW(4, 2, DMA_WINDOW_START)] + LI(4, start + Z_BYTES) + [SW(4, 2, DMA_WINDOW_END)]
        words += self.clear(1, start)                              # exactly the window
        words += self.clear(2, start + 4)                          # one word past END
        words += self.clear(3, start - 4)                          # one word before START
        words += self.clear(4, RAM + 0x100000)                     # elsewhere in RAM
        # START validates ZBASE as CLEAR_Z does: outside the window it is E_PARAM before any work.
        words += LI(4, start - 4) + [SW(4, 1, G3D_ZBASE)] + LI(4, 1) + [SW(4, 1, G3D_VCOUNT), SW(0, 1, G3D_TCOUNT)]
        words += LI(4, 1000) + [SW(4, 1, G3D_LIMIT)] + LI(4, G3D_START) + [SW(4, 1, G3D_COMMAND)]
        words += wait(1, G3D_STATUS) + record(12, 1, G3D_STATUS) + record(13, 1, G3D_ERROR)
        # A job validated inside the window runs to the end even if the window then shrinks to
        # nothing: the engines only test it when a job starts.
        words += LI(4, start) + [SW(4, 1, G3D_ZBASE)] + LI(4, G3D_CLEAR_Z) + [SW(4, 1, G3D_COMMAND)]
        words += [ADDI(0, 0, 0)] * 4 + [SW(0, 2, DMA_WINDOW_START), SW(0, 2, DMA_WINDOW_END)]
        words += wait(1, G3D_STATUS) + record(10, 1, G3D_STATUS) + record(11, 1, G3D_ERROR)
        words += FINISH()
        done, param = (G3D_DONE, 0), (G3D_FAULT, G3D_E_PARAM)
        for run in self.run_pair(words, "g2-window", seed=5, gpu_seed=9):
            stored = self.stored(run)
            self.assertEqual([(stored[2 * i], stored[2 * i + 1]) for i in range(7)],
                             [done, done, param, param, param, done, param])
            self.assertEqual(self.traps(run), [])


if __name__ == "__main__":
    unittest.main()
