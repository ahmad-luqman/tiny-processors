"""Tests for the GDB remote-protocol stub in the headless emulator (tools/rv32_gdb.c).

No gdb and no network beyond loopback: a small RSP client below (framing, checksums, acks,
binary escapes) drives `rv32emu --gdb 0` against the self-check image and against programs
assembled with tools/rv32_asm.py. Expected values come from the emulator's own --trace of the
same image run without the stub, from the image file, and from hand-computed constants. One
optional test runs a real gdb (gdb-multiarch, riscv64-elf-gdb, or a plain gdb that knows
riscv:rv32) in batch mode and skips, saying why, when none is on PATH. Only the tests that need
the self-check's own program (its trace, symbols, or console) skip when it is not built.
"""

import os
from pathlib import Path
import re
import select
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import time
import unittest
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools.rv32_asm import (  # noqa: E402
    ADDI, BNE, CSRRW, ECALL, FB, FINISH, INPUT, JAL, LI, LW, MCAUSE, MTVEC, RAM, SW, UNMAPPED, words_to_bytes)
from tools.rv32_run_emu import build_emulator, emulator_command, halt_line  # noqa: E402

SELFCHECK = ROOT / "build/rv32/selfcheck.bin"
SELFCHECK_ELF = ROOT / "build/rv32/selfcheck.elf"
# The pass word the Makefile pins for the self-check (as test_rv32_emu.py reads RV32_PONG_HEX).
SELFCHECK_CONSOLE = "PASS " + re.search(r"^RV32_SELFCHECK_HEX := ([0-9a-f]{8})$",
                                        (ROOT / "Makefile").read_text(), re.M).group(1)
REG_PC, REG_F0, REG_CSR0 = 32, 33, 65
PACKET_SIZE = 0x4000
MAX_READ = (PACKET_SIZE - 16) // 2  # the stub's longest m reply, in bytes
MAX_BREAKPOINTS = 64
LISTENING = re.compile(r"^rv32emu: gdb listening on 127\.0\.0\.1:(\d+)$")
TIMEOUT = 30


def checksum(payload):
    return sum(payload) & 0xFF


def frame(payload):
    return b"$" + payload + b"#" + f"{checksum(payload):02x}".encode()


def unescape(data):
    """Undo the `}` escapes of a binary reply (qXfer)."""
    out, i = bytearray(), 0
    while i < len(data):
        if data[i] == 0x7D:
            out.append(data[i + 1] ^ 0x20)
            i += 2
        else:
            out.append(data[i])
            i += 1
    return bytes(out)


def reg_hex(value):
    """A 32-bit register in the stub's little-endian hex."""
    return (value & 0xFFFFFFFF).to_bytes(4, "little").hex()


def reg_value(text):
    return int.from_bytes(bytes.fromhex(text), "little")


def riscv_capable_gdb():
    """A plain `gdb` on PATH built with the RISC-V target (Homebrew's is multi-target), or None."""
    gdb = shutil.which("gdb")
    if gdb is None:
        return None
    try:
        probe = subprocess.run([gdb, "-nx", "-batch", "-ex", "set architecture riscv:rv32"],
                               capture_output=True, text=True, timeout=TIMEOUT)
    except (OSError, subprocess.TimeoutExpired):
        return None
    output = probe.stdout + probe.stderr
    ok = probe.returncode == 0 and 'architecture is set to "riscv:rv32"' in output
    return gdb if ok else None


class Client:
    """The debugger side of the remote serial protocol, strict about what the stub sends."""

    def __init__(self, sock):
        self.sock = sock
        self.pending = b""
        self.ack = True

    def byte(self):
        while not self.pending:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise EOFError("stub closed the connection")
            self.pending += chunk
        value, self.pending = self.pending[0], self.pending[1:]
        return value

    def send_raw(self, data):
        self.sock.sendall(data)

    def send(self, payload):
        """Send one packet and, in ack mode, require the stub's `+`."""
        if isinstance(payload, str):
            payload = payload.encode()
        self.send_raw(frame(payload))
        if self.ack:
            got = self.byte()
            if got != ord("+"):
                raise AssertionError(f"expected + for {payload!r}, got {bytes([got])!r}")

    def receive(self):
        """Read one reply, check its checksum, acknowledge it, and return the payload bytes."""
        if (c := self.byte()) != ord("$"):
            raise AssertionError(f"unexpected byte {bytes([c])!r} before a reply")
        payload = bytearray()
        while (c := self.byte()) != ord("#"):
            payload.append(c)
        digits = bytes([self.byte(), self.byte()])
        if int(digits, 16) != checksum(payload):
            raise AssertionError(f"bad checksum {digits!r} on {bytes(payload)!r}")
        if self.ack:
            self.send_raw(b"+")
        return bytes(payload)

    def ask(self, payload):
        """One request, one reply, as text."""
        self.send(payload)
        return self.receive().decode("latin-1")

    def registers(self):
        text = self.ask("g")
        assert len(text) == 65 * 8, text
        return [reg_value(text[i:i + 8]) for i in range(0, len(text), 8)]

    def reg(self, number):
        text = self.ask(f"p{number:x}")
        assert len(text) == 8, text
        return reg_value(text)

    def read(self, addr, length):
        return self.ask(f"m{addr:x},{length:x}")


class Session:
    """One `rv32emu --gdb 0` process and a connected client."""

    def __init__(self, emulator, image, *extra):
        self.process = subprocess.Popen(emulator_command(emulator, image) + ["--gdb", "0", *map(str, extra)],
                                        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        line = self._stderr_line()
        match = LISTENING.match(line)
        if not match:
            self.process.kill()
            raise AssertionError(f"no listening line: {line!r} {self.process.stderr.read()!r}")
        self.first_line = line
        self.port = int(match.group(1))
        self.sock = socket.create_connection(("127.0.0.1", self.port), timeout=TIMEOUT)
        self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self.client = Client(self.sock)

    def _stderr_line(self):
        deadline = time.monotonic() + TIMEOUT
        line = b""
        while not line.endswith(b"\n"):
            ready, _, _ = select.select([self.process.stderr], [], [], max(0.0, deadline - time.monotonic()))
            if not ready:
                raise AssertionError("the emulator did not print its listening line")
            byte = os.read(self.process.stderr.fileno(), 1)
            if not byte:
                break
            line += byte
        return line.decode().rstrip("\n")

    def finish(self):
        """Close our end, wait for the process, and return (status, stdout, stderr)."""
        self.sock.close()
        stdout, stderr = self.process.communicate(timeout=TIMEOUT)
        return self.process.returncode, stdout.decode(), self.first_line + "\n" + stderr.decode()


class GdbStubTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.workdir = tempfile.TemporaryDirectory()
        cls.dir = Path(cls.workdir.name)
        cls.emulator = cls.dir / "rv32emu"
        build_emulator(cls.emulator)
        cls.plain = None

    @classmethod
    def tearDownClass(cls):
        cls.workdir.cleanup()

    def setUp(self):
        self.sessions = []

    def tearDown(self):
        for session in self.sessions:
            if session.process.poll() is None:
                session.process.kill()
                session.process.communicate()
            session.sock.close()

    def start(self, image, *extra):
        session = Session(self.emulator, image, *extra)
        self.sessions.append(session)
        return session, session.client

    def need_selfcheck(self):
        if not SELFCHECK.exists():
            self.skipTest("build/rv32/selfcheck.bin is missing (make check-rv32-image)")

    def image(self, words, name="image.bin"):
        path = self.dir / f"{self._testMethodName}-{name}"
        path.write_bytes(words_to_bytes(words))
        return path

    def tiny(self):
        """A few instructions and the pass finish: for protocol tests that need no real program,
        so they never skip for a missing self-check build."""
        return self.image([ADDI(5, 0, 1), ADDI(5, 5, 1), ADDI(5, 5, 1), ADDI(5, 5, 1)] + FINISH())

    def plain_run(self):
        """The self-check without the stub: console, stderr, status, and the trace lines."""
        if GdbStubTest.plain is None:
            trace = self.dir / "plain.trace"
            completed = subprocess.run(emulator_command(self.emulator, SELFCHECK, trace=trace),
                                       capture_output=True, text=True, timeout=TIMEOUT)
            GdbStubTest.plain = (completed.returncode, completed.stdout, completed.stderr,
                                 trace.read_text().splitlines())
        return GdbStubTest.plain

    def assert_exit(self, session, status, halt, outcome=None):
        code, stdout, stderr = session.finish()
        parsed = halt_line(stderr)
        self.assertEqual(code, status, stderr)
        self.assertIsNotNone(parsed, stderr)
        self.assertEqual(parsed["halt"], halt, stderr)
        if outcome is not None:
            self.assertIn(outcome, parsed["outcome"], stderr)
        return stdout, stderr

    # The description and the initial stop.

    def test_target_description(self):
        """qSupported advertises what the stub serves, and target.xml, read in small chunks to
        exercise the m/l continuation, is XML with gdb's RISC-V features and register numbers."""
        session, client = self.start(self.tiny())
        features = client.ask("qSupported:multiprocess+;swbreak+;hwbreak+;xmlRegisters=i386").split(";")
        self.assertIn("qXfer:features:read+", features)
        self.assertIn("swbreak+", features)
        self.assertTrue(any(re.fullmatch(r"PacketSize=[0-9a-f]+", f) for f in features), features)
        xml, offset = b"", 0
        while True:
            client.send(f"qXfer:features:read:target.xml:{offset:x},100")
            chunk = client.receive()
            self.assertIn(chunk[:1], (b"m", b"l"))
            data = unescape(chunk[1:])
            xml += data
            offset += len(data)
            if chunk[:1] == b"l":
                break
            self.assertEqual(len(data), 0x100)
        self.assertEqual(client.ask("qXfer:features:read:other.xml:0,100"), "E00")
        root = ET.fromstring(xml)
        self.assertEqual(root.findtext("architecture"), "riscv:rv32")
        features = {f.get("name"): f.findall("reg") for f in root.findall("feature")}
        cpu = features["org.gnu.gdb.riscv.cpu"]
        self.assertEqual(len(cpu), 33)
        self.assertEqual([int(r.get("regnum")) for r in cpu], list(range(33)))
        self.assertEqual((cpu[0].get("name"), cpu[32].get("name")), ("zero", "pc"))
        self.assertTrue(all(r.get("bitsize") == "32" for r in cpu))
        fpu = {r.get("name"): r for r in features["org.gnu.gdb.riscv.fpu"]}
        floats = [r for r in fpu.values() if r.get("type") == "ieee_single"]
        self.assertEqual(sorted(int(r.get("regnum")) for r in floats), list(range(33, 65)))
        self.assertEqual({n: int(fpu[n].get("regnum")) for n in ("fflags", "frm", "fcsr")},
                         {"fflags": 66, "frm": 67, "fcsr": 68})
        csr = {r.get("name"): int(r.get("regnum")) for r in features["org.gnu.gdb.riscv.csr"]}
        self.assertEqual(csr, {"mstatus": 65 + 0x300, "mie": 65 + 0x304, "mscratch": 65 + 0x340, "mip": 65 + 0x344, "mtvec": 65 + 0x305, "mepc": 65 + 0x341, "mcause": 65 + 0x342, "mtval": 65 + 0x343,
                               "cycle": 65 + 0xC00, "time": 65 + 0xC01, "instret": 65 + 0xC02,
                               "cycleh": 65 + 0xC80, "timeh": 65 + 0xC81, "instreth": 65 + 0xC82})
        client.send("k")
        self.assert_exit(session, 2, "stopped", "error=host-stopped")

    def test_stopped_at_reset(self):
        """The machine waits at the reset pc with zeroed registers except a1, the boot convention's
        device-tree address; the thread queries have their one-hart answers and an unknown packet
        gets the empty reply."""
        session, client = self.start(self.tiny())
        self.assertEqual(client.ask("?"), "T05")
        regs = client.registers()
        self.assertEqual(regs[REG_PC], 0x80000000)
        self.assertEqual(regs[:32], [0] * 11 + [0x1000] + [0] * 20)
        self.assertEqual(client.reg(REG_PC), 0x80000000)
        self.assertEqual(client.ask("qAttached"), "1")
        self.assertEqual(client.ask("qC"), "QC1")
        self.assertEqual(client.ask("qfThreadInfo"), "m1")
        self.assertEqual(client.ask("qsThreadInfo"), "l")
        self.assertEqual(client.ask("Hg0"), "OK")
        self.assertEqual(client.ask("Hc-1"), "OK")
        self.assertEqual(client.ask("vCont?"), "vCont;c;C;s;S")
        self.assertEqual(client.ask("vMustReplyEmpty"), "")
        self.assertEqual(client.ask("qNoSuchThing"), "")
        self.assertEqual(client.ask("Z2,80000000,4"), "", "watchpoints are not supported")
        self.assertEqual(client.ask("p9999"), "E01")
        self.assertEqual(client.ask(f"p{REG_CSR0 + 0x7C0:x}"), "E01", "no CSR 0x7c0")
        self.assertEqual(client.reg(REG_CSR0 + 0x300), 0x80007800, "mstatus at reset: MPP, FS and SD read as constants")
        client.send("k")
        self.assert_exit(session, 2, "stopped")

    # Stepping, breakpoints, continuing.

    def test_single_steps_follow_the_trace(self):
        """300 single steps visit the trace's pcs in order, and every register the trace says an
        instruction wrote holds that value after the step."""
        self.need_selfcheck()
        trace = self.plain_run()[3]
        session, client = self.start(SELFCHECK)
        self.assertEqual(client.reg(REG_PC), int(trace[0].split()[1], 16))
        for i in range(300):
            self.assertEqual(client.ask("s" if i % 2 else "vCont;s:1"), "T05")
            regs = client.registers()
            self.assertEqual(regs[REG_PC], int(trace[i + 1].split()[1], 16), f"after step {i + 1}")
            for effect in trace[i].split()[3:]:
                if m := re.fullmatch(r"x(\d+)=([0-9a-f]{8})", effect):
                    self.assertEqual(regs[int(m.group(1))], int(m.group(2), 16), trace[i])
        client.send("k")
        self.assert_exit(session, 2, "stopped")

    def test_trace_under_gdb_is_the_plain_trace(self):
        """Steps, a breakpoint stop, and a continue to the end write exactly the plain trace."""
        self.need_selfcheck()
        status, stdout, _, trace = self.plain_run()
        traced = self.dir / "gdb.trace"
        session, client = self.start(SELFCHECK, "--trace", traced)
        for _ in range(10):
            self.assertEqual(client.ask("s"), "T05")
        target = int(trace[500].split()[1], 16)
        self.assertEqual(client.ask(f"Z0,{target:x},4"), "OK")
        self.assertEqual(client.ask("c"), "T05")
        self.assertEqual(client.reg(REG_PC), target)
        self.assertEqual(client.ask(f"z0,{target:x},4"), "OK")
        self.assertEqual(client.ask("c"), "W00")
        out, _ = self.assert_exit(session, 0, "done", "pass")
        self.assertEqual(out, stdout)
        self.assertEqual(traced.read_text().splitlines(), trace)

    def test_breakpoint_hits_every_call(self):
        """A breakpoint on fib stops once per execution of its first instruction, as the trace
        counts them, with the swbreak stop reason; after z0 the run continues to W00."""
        self.need_selfcheck()
        nm = os.environ.get("RV32_NM")  # the Makefile passes its RV32_NM
        nm = nm if nm and Path(nm).exists() else shutil.which("llvm-nm")
        if nm is None or not SELFCHECK_ELF.exists():
            self.skipTest("llvm-nm or build/rv32/selfcheck.elf is missing")
        symbols = subprocess.run([nm, str(SELFCHECK_ELF)], capture_output=True, text=True, check=True).stdout
        fib = next(int(line.split()[0], 16) for line in symbols.splitlines() if line.split()[-1] == "fib")
        trace = self.plain_run()[3]
        calls = sum(1 for line in trace if int(line.split()[1], 16) == fib)
        self.assertGreater(calls, 100)
        session, client = self.start(SELFCHECK)
        client.ask("qSupported:swbreak+;hwbreak+")
        self.assertEqual(client.ask(f"Z0,{fib:x},4"), "OK")
        self.assertEqual(client.ask(f"Z0,{fib:x},4"), "OK", "inserting twice is idempotent")
        hits = 0
        while (reply := client.ask("c" if hits % 2 else "vCont;c")) == "T05swbreak:;":
            hits += 1
            self.assertEqual(client.reg(REG_PC), fib)
            if hits == calls:
                break
        self.assertEqual(hits, calls, reply)
        self.assertEqual(client.ask(f"z0,{fib:x},4"), "OK")
        self.assertEqual(client.ask(f"z0,{fib:x},4"), "E01", "already removed")
        self.assertEqual(client.ask("c"), "W00")
        stdout, _ = self.assert_exit(session, 0, "done", "pass")
        self.assertEqual(stdout.strip(), SELFCHECK_CONSOLE)

    def test_breakpoints_in_an_assembled_program(self):
        """Breakpoints by instruction index: the stop is before the instruction executes, a
        hardware breakpoint shares the table and says hwbreak, and removing one lets the run pass."""
        words = [ADDI(5, 0, 1), ADDI(5, 5, 1), ADDI(5, 5, 1), ADDI(5, 5, 1), ADDI(5, 5, 1)] + FINISH()
        session, client = self.start(self.image(words))
        client.ask("qSupported:swbreak+;hwbreak+")
        self.assertEqual(client.ask(f"Z0,{RAM + 8:x},4"), "OK")
        self.assertEqual(client.ask(f"Z1,{RAM + 16:x},4"), "OK")
        self.assertEqual(client.ask("c"), "T05swbreak:;")
        self.assertEqual((client.reg(REG_PC), client.reg(5)), (RAM + 8, 2))
        self.assertEqual(client.ask("c"), "T05hwbreak:;")
        self.assertEqual((client.reg(REG_PC), client.reg(5)), (RAM + 16, 4))
        self.assertEqual(client.ask(f"z1,{RAM + 16:x},4"), "OK")
        self.assertEqual(client.ask("c"), "W00")
        self.assert_exit(session, 0, "done", "pass")

    def test_breakpoint_at_the_stopped_pc_stops_before_executing(self):
        """A breakpoint where the machine already stands, and was not stopped by, is reported
        before anything executes: at reset, at a pc written with P, at a c ADDR target, and after
        a step. Only the continue from the breakpoint's own stop steps over it, and a loop through
        the breakpoint stops there again on each pass."""
        words = [ADDI(5, 0, 1), ADDI(5, 5, 1), ADDI(5, 5, 1), ADDI(5, 5, 1)] + FINISH()
        session, client = self.start(self.image(words))
        self.assertEqual(client.ask(f"Z0,{RAM:x},4"), "OK")
        self.assertEqual(client.ask("c"), "T05", "the breakpoint at reset stops the first continue")
        self.assertEqual((client.reg(REG_PC), client.reg(5)), (RAM, 0), "nothing executed")
        self.assertEqual(client.ask(f"Z0,{RAM + 8:x},4"), "OK")
        self.assertEqual(client.ask("c"), "T05", "stepped over the reset breakpoint, stopped at the next")
        self.assertEqual((client.reg(REG_PC), client.reg(5)), (RAM + 8, 2))
        # A pc written with P is not the breakpoint that stopped the machine: it is checked.
        self.assertEqual(client.ask(f"P{REG_PC:x}={reg_hex(RAM)}"), "OK")
        self.assertEqual(client.ask("c"), "T05")
        self.assertEqual((client.reg(REG_PC), client.reg(5)), (RAM, 2), "stopped at reset again, nothing ran")
        # So is a resume address, even the one the machine already stands on after a step.
        self.assertEqual(client.ask("s"), "T05")
        self.assertEqual(client.ask(f"c{RAM + 8:x}"), "T05")
        self.assertEqual((client.reg(REG_PC), client.reg(5)), (RAM + 8, 1))
        self.assertEqual(client.ask(f"z0,{RAM:x},4"), "OK")
        self.assertEqual(client.ask(f"Z0,{RAM + 12:x},4"), "OK")
        self.assertEqual(client.ask("s"), "T05", "a step always executes, breakpoint or not")
        self.assertEqual(client.ask("c"), "T05", "after a step the breakpoint at the new pc is checked")
        self.assertEqual((client.reg(REG_PC), client.reg(5)), (RAM + 12, 2))
        self.assertEqual(client.ask(f"z0,{RAM + 8:x},4"), "OK")
        self.assertEqual(client.ask(f"z0,{RAM + 12:x},4"), "OK")
        self.assertEqual(client.ask("c"), "W00")
        self.assert_exit(session, 0, "done", "pass")

    def test_breakpoint_in_a_loop_hits_every_pass(self):
        """Continuing from a breakpoint steps over it once; the next pass stops there again."""
        words = [ADDI(6, 0, 3), ADDI(6, 6, -1), BNE(6, 0, -4)] + FINISH()
        session, client = self.start(self.image(words))
        self.assertEqual(client.ask(f"Z0,{RAM + 4:x},4"), "OK")
        counts = []
        for _ in range(3):
            self.assertEqual(client.ask("c"), "T05")
            counts.append(client.reg(6))
        self.assertEqual(counts, [3, 2, 1])
        self.assertEqual(client.ask("c"), "W00")
        self.assert_exit(session, 0, "done", "pass")

    def test_breakpoint_table_full(self):
        """64 breakpoints fit; the 65th is refused with E01, re-inserting one already there is
        still OK, and removing one makes room again."""
        session, client = self.start(self.tiny())
        for i in range(MAX_BREAKPOINTS):
            self.assertEqual(client.ask(f"Z0,{RAM + 0x1000 + 4 * i:x},4"), "OK")
        self.assertEqual(client.ask(f"Z0,{RAM + 0x2000:x},4"), "E01", "the table is full")
        self.assertEqual(client.ask(f"Z1,{RAM + 0x1000:x},4"), "E01", "Z1 shares the table")
        self.assertEqual(client.ask(f"Z0,{RAM + 0x1000:x},4"), "OK", "already there")
        self.assertEqual(client.ask(f"z0,{RAM + 0x1000:x},4"), "OK")
        self.assertEqual(client.ask(f"Z0,{RAM + 0x2000:x},4"), "OK")
        self.assertEqual(client.ask("c"), "W00")
        self.assert_exit(session, 0, "done", "pass")

    def test_step_into_a_trap(self):
        """A trapping instruction is one step: pc lands on mtvec, and mcause reads through p."""
        handler = RAM + 0x100
        words = LI(5, handler) + [CSRRW(0, MTVEC, 5), ECALL()] + FINISH(0x00013333)
        words += [0] * ((handler - RAM) // 4 - len(words)) + FINISH()
        session, client = self.start(self.image(words))
        for _ in range(3):
            self.assertEqual(client.ask("s"), "T05")
        self.assertEqual(client.reg(REG_PC), RAM + 12)
        self.assertEqual(client.ask("s"), "T05")
        self.assertEqual(client.reg(REG_PC), handler)
        self.assertEqual(client.reg(REG_CSR0 + MCAUSE), 11)
        self.assertEqual(client.reg(REG_CSR0 + 0x341), RAM + 12)
        self.assertEqual(client.ask("c"), "W00")
        self.assert_exit(session, 0, "done", "pass")

    def test_ctrl_c_interrupts_a_loop(self):
        """0x03 while a self-loop runs stops it with SIGINT at the loop; continue and interrupt
        again, then kill: exit 2 with the host-stopped halt."""
        words = [ADDI(5, 0, 7), JAL(0, 0)]
        session, client = self.start(self.image(words), "--max-instructions", 10 ** 15)
        for _ in range(2):
            client.send("c")
            time.sleep(0.2)
            client.send_raw(b"\x03")
            self.assertEqual(client.receive(), b"T02")
            self.assertEqual(client.reg(REG_PC), RAM + 4)
        self.assertEqual(client.reg(5), 7)
        client.send("k")
        self.assert_exit(session, 2, "stopped", "error=host-stopped")

    def test_ctrl_c_with_a_breakpoint_elsewhere(self):
        """The step-by-step path (a breakpoint is set) also polls for Ctrl-C."""
        words = [JAL(0, 0)] + FINISH()
        session, client = self.start(self.image(words), "--max-instructions", 10 ** 15)
        self.assertEqual(client.ask(f"Z0,{RAM + 4:x},4"), "OK")
        client.send("c")
        time.sleep(0.2)
        client.send_raw(b"\x03")
        self.assertEqual(client.receive(), b"T02")
        client.send("k")
        self.assert_exit(session, 2, "stopped")

    # Exits.

    def test_continue_to_pass(self):
        """The self-check under the stub: W00, the process exits 0 with the same console and
        halt line as a plain run."""
        self.need_selfcheck()
        status, stdout, stderr, _ = self.plain_run()
        session, client = self.start(SELFCHECK)
        self.assertEqual(client.ask("c"), "W00")
        out, err = self.assert_exit(session, 0, "done", "pass")
        self.assertEqual((out, status), (stdout, 0))
        self.assertEqual(out.strip(), SELFCHECK_CONSOLE)
        self.assertEqual(halt_line(err), halt_line(stderr))

    def test_fail_word_reports_its_code(self):
        session, client = self.start(self.image(FINISH(0x00013333)))
        self.assertEqual(client.ask("c"), "W01")
        self.assert_exit(session, 1, "done", "fail=1")

    def test_halt_by_limit_reports_error(self):
        """The instruction limit is an emulator error: W02. emu_run_until checks the limit before
        the budget, so the step that executes the last allowed instruction already ends the session."""
        session, client = self.start(self.image([JAL(0, 0)]), "--max-instructions", 3)
        for _ in range(2):
            self.assertEqual(client.ask("s"), "T05")
        self.assertEqual(client.ask("s"), "W02")
        self.assert_exit(session, 2, "limit", "error=instruction-limit")

    def test_detach_runs_to_completion(self):
        self.need_selfcheck()
        session, client = self.start(SELFCHECK)
        self.assertEqual(client.ask("s"), "T05")
        self.assertEqual(client.ask("D"), "OK")
        stdout, _ = self.assert_exit(session, 0, "done", "pass")
        self.assertEqual(stdout.strip(), SELFCHECK_CONSOLE)

    def test_hang_up_stops_the_run(self):
        session, client = self.start(self.tiny())
        self.assertEqual(client.ask("s"), "T05")
        _, stderr = self.assert_exit(session, 2, "stopped", "error=host-stopped")
        self.assertIn("rv32emu: gdb: connection closed by client\n", stderr, "a hang-up says so, unlike k")

    def test_reset_connection_says_why(self):
        """A connection that fails (here a reset) ends the run as a hang-up does, with the error
        on stderr, so it is not mistaken for a deliberate kill."""
        session, client = self.start(self.tiny())
        self.assertEqual(client.ask("s"), "T05")
        session.sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
        _, stderr = self.assert_exit(session, 2, "stopped", "error=host-stopped")
        self.assertRegex(stderr, r"rv32emu: gdb: connection (lost: .+|closed by client)\n")

    def test_vkill(self):
        session, client = self.start(self.tiny())
        self.assertEqual(client.ask("vKill;1"), "OK")
        _, stderr = self.assert_exit(session, 2, "stopped")
        self.assertNotIn("gdb: connection", stderr, "a kill is not a lost connection")

    # Memory.

    def test_memory_reads_the_image(self):
        """m returns the loaded image byte for byte (in chunks), zeros past it and in the
        framebuffer, and E01 outside RAM and the framebuffer or across RAM's end."""
        self.need_selfcheck()
        image = SELFCHECK.read_bytes()
        session, client = self.start(SELFCHECK)
        data = b""
        while len(data) < len(image):
            data += bytes.fromhex(client.read(RAM + len(data), min(0x700, len(image) - len(data))))
        self.assertEqual(data, image)
        self.assertEqual(client.read(RAM + 0x3FFFFC, 4), "00000000")
        self.assertEqual(client.read(FB, 8), "00" * 8)
        self.assertEqual(client.read(UNMAPPED, 4), "E01")
        self.assertEqual(client.read(RAM + 0x3FFFFE, 4), "E01")
        self.assertEqual(client.read(0xFFFFFFFE, 4), "E01", "a wrapping range is refused")
        self.assertEqual(client.read(RAM - 2, 4), "E01")
        self.assertEqual(client.ask("m80000000"), "E01", "no length")
        self.assertEqual(client.ask("mzz,4"), "E01")
        client.send("k")
        self.assert_exit(session, 2, "stopped")

    def test_long_read_is_clamped(self):
        """A read longer than a reply can carry returns the first MAX_READ bytes, a short reply
        the protocol allows (gdb asks again for the rest)."""
        words = [ADDI(5, 0, 1)] + FINISH()
        session, client = self.start(self.image(words))
        data = bytes.fromhex(client.read(RAM, PACKET_SIZE))
        self.assertEqual(len(data), MAX_READ)
        image = words_to_bytes(words)
        self.assertEqual(data, image + bytes(MAX_READ - len(image)))
        client.send("k")
        self.assert_exit(session, 2, "stopped")

    def test_memory_writes(self):
        """M and X (with every escaped byte) write RAM and the framebuffer; device windows and
        malformed writes are refused and leave no trace on the console."""
        words = [ADDI(0, 0, 0)] + FINISH()
        session, client = self.start(self.image(words))
        self.assertEqual(client.ask(f"M{RAM + 0x1000:x},4:efbeadde"), "OK")
        self.assertEqual(client.read(RAM + 0x1000, 4), "efbeadde")
        payload = bytes([0x23, 0x24, 0x7D, 0x2A, 0x00, 0xFF])  # # $ } * need escaping
        escaped = b"".join(bytes([0x7D, b ^ 0x20]) if b in b"#$}*" else bytes([b]) for b in payload)
        client.send(f"X{RAM + 0x2000:x},{len(payload):x}:".encode() + escaped)
        self.assertEqual(client.receive(), b"OK")
        self.assertEqual(bytes.fromhex(client.read(RAM + 0x2000, len(payload))), payload)
        self.assertEqual(client.ask(f"X{RAM:x},0:"), "OK", "gdb's X probe")
        self.assertEqual(client.ask(f"M{FB + 10:x},2:abcd"), "OK")
        self.assertEqual(client.read(FB + 10, 2), "abcd")
        self.assertEqual(client.ask("M10000000,1:41"), "E01", "the console is a device")
        self.assertEqual(client.ask(f"M{UNMAPPED:x},1:41"), "E01")
        self.assertEqual(client.ask(f"M{RAM:x},2:41"), "E01", "too few bytes")
        self.assertEqual(client.ask(f"M{RAM:x},1:4g"), "E01")
        self.assertEqual(client.ask(f"X{RAM:x},4:ab"), "E01", "X with fewer bytes than its length")
        self.assertEqual(client.ask(f"X{RAM:x},1:abc"), "E01", "X with more bytes than its length")
        self.assertEqual(client.ask("c"), "W00")
        stdout, _ = self.assert_exit(session, 0, "done")
        self.assertEqual(stdout, "")

    def test_memory_write_changes_execution(self):
        """Patching the next instruction through M changes what executes."""
        words = [ADDI(5, 0, 1)] + FINISH()
        session, client = self.start(self.image(words))
        self.assertEqual(client.ask(f"M{RAM:x},4:{reg_hex(ADDI(5, 0, 9))}"), "OK")
        self.assertEqual(client.ask("s"), "T05")
        self.assertEqual(client.reg(5), 9)
        client.send("k")
        self.assert_exit(session, 2, "stopped")

    def test_input_device_is_not_read(self):
        """A debugger read of the input window is refused and pops nothing: the guest still sees
        both frame-0 events queued."""
        words = LI(1, INPUT) + [LW(5, 1, 4), ADDI(6, 0, 2), BNE(5, 6, 24)] + FINISH() + FINISH(0x00013333)
        script = self.dir / "input.txt"
        script.write_text("frame 0 down LEFT\nframe 0 up LEFT\n")
        session, client = self.start(self.image(words), "--input", script)
        for offset in (0, 4, 8):
            self.assertEqual(client.read(INPUT + offset, 4), "E01")
        self.assertEqual(client.ask(f"M{INPUT:x},4:00000000"), "E01")
        self.assertEqual(client.read(0x10000005, 1), "E01", "console status is a device too")
        self.assertEqual(client.ask("c"), "W00")
        self.assert_exit(session, 0, "done", "pass")

    # Registers.

    def test_register_writes(self):
        """P and G: x0 stays zero, pc moves, f registers take any bits, CSRs keep their WARL
        masks, and a written register changes the guest's done word."""
        words = LI(31, 0x00013333) + LI(30, 0x00100000) + [SW(31, 30, 0)]
        session, client = self.start(self.image(words))
        self.assertEqual(client.ask(f"P0={reg_hex(0x12345678)}"), "OK")
        self.assertEqual(client.reg(0), 0)
        self.assertEqual(client.ask(f"P{REG_F0 + 3:x}={reg_hex(0x3FC00000)}"), "OK")
        self.assertEqual(client.reg(REG_F0 + 3), 0x3FC00000)
        self.assertEqual(client.ask(f"P{REG_CSR0 + MTVEC:x}={reg_hex(0x80000123)}"), "OK")
        self.assertEqual(client.reg(REG_CSR0 + MTVEC), 0x80000120)
        self.assertEqual(client.ask(f"P{REG_CSR0 + 0x341:x}={reg_hex(0xFFFFFFFF)}"), "OK")
        self.assertEqual(client.reg(REG_CSR0 + 0x341), 0xFFFFFFFC)
        self.assertEqual(client.ask(f"P{REG_CSR0 + 3:x}={reg_hex(0xFFFFFFFF)}"), "OK")
        self.assertEqual((client.reg(REG_CSR0 + 3), client.reg(REG_CSR0 + 1), client.reg(REG_CSR0 + 2)),
                         (0xFF, 0x1F, 7))
        self.assertEqual(client.ask(f"P{REG_CSR0 + 2:x}={reg_hex(0)}"), "OK")
        self.assertEqual(client.reg(REG_CSR0 + 3), 0x1F)
        self.assertEqual(client.ask(f"P{REG_CSR0 + 0x7C0:x}={reg_hex(0)}"), "E01", "no CSR 0x7c0")
        self.assertEqual(client.ask(f"P{REG_CSR0 + 0x300:x}={reg_hex(0xFFFFFFFF)}"), "OK")
        self.assertEqual(client.reg(REG_CSR0 + 0x300), 0x807E79AA,
                         "only the S and M enables, SPP, MPRV, SUM, MXR, TVM, TW and TSR are writable")
        self.assertEqual(client.ask(f"P{REG_CSR0 + 0x300:x}={reg_hex(0)}"), "OK")
        self.assertEqual(client.ask(f"P5={reg_hex(1)[:6]}"), "E01", "short value")
        # G writes the whole block: keep everything but x7.
        regs = client.registers()
        regs[7] = 0xCAFEF00D
        self.assertEqual(client.ask("G" + "".join(reg_hex(v) for v in regs)), "OK")
        self.assertEqual(client.reg(7), 0xCAFEF00D)
        self.assertEqual(client.ask("G123"), "E01")
        # Run the two LIs, then make x31 the pass word before the store.
        for _ in range(4):
            self.assertEqual(client.ask("s"), "T05")
        self.assertEqual(client.reg(31), 0x00013333)
        self.assertEqual(client.ask(f"P1f={reg_hex(0x5555)}"), "OK")
        self.assertEqual(client.ask("c"), "W00")
        self.assert_exit(session, 0, "done", "pass")

    def test_counters_read_and_refuse_writes(self):
        """The Zicntr counters are registers too: instret counts the steps taken, cycle and time
        count device ticks (instructions on the emulator), and a write is refused as csrw traps."""
        session, client = self.start(self.image([ADDI(1, 0, 1)] * 4 + FINISH()))
        for _ in range(3):
            self.assertEqual(client.ask("s"), "T05")
        self.assertEqual([client.reg(REG_CSR0 + number) for number in (0xC00, 0xC01, 0xC02, 0xC80, 0xC81, 0xC82)],
                         [3, 3, 3, 0, 0, 0])
        self.assertEqual(client.ask(f"P{REG_CSR0 + 0xC02:x}={reg_hex(0)}"), "E01", "instret is read-only")
        self.assertEqual(client.reg(REG_CSR0 + 0xC02), 3)
        self.assertEqual(client.ask("c"), "W00")
        self.assert_exit(session, 0, "done", "pass")

    def test_pc_write_and_resume_address(self):
        """P on pc, and c/s with an address, move execution: skipping the fail finish passes."""
        words = FINISH(0x00013333) + FINISH()
        session, client = self.start(self.image(words))
        self.assertEqual(client.ask(f"P{REG_PC:x}={reg_hex(RAM + 20)}"), "OK")
        self.assertEqual(client.ask(f"s{RAM + 20:x}"), "T05")
        self.assertEqual(client.reg(REG_PC), RAM + 24)
        self.assertEqual(client.ask(f"c{RAM + 20:x}"), "W00")
        self.assert_exit(session, 0, "done", "pass")

    # Framing.

    def test_bad_checksum_and_malformed_packets(self):
        """A bad checksum or a non-hex one gets `-` and no reply; the next good packet works. A
        well-framed but malformed request gets E01."""
        session, client = self.start(self.tiny())
        client.send_raw(b"$g#00")
        self.assertEqual(client.byte(), ord("-"))
        client.send_raw(b"$g#zz")
        self.assertEqual(client.byte(), ord("-"))
        client.send_raw(b"+++")  # stray acks between packets are ignored
        self.assertEqual(client.registers()[REG_PC], RAM)
        self.assertEqual(client.ask("m80000000,"), "E01")
        self.assertEqual(client.ask("Z0,80000000"), "E01")
        self.assertEqual(client.ask("P20"), "E01")
        self.assertEqual(client.ask("vCont;x"), "E01")
        self.assertEqual(client.ask("m1ffffffff,4"), "E01", "more than eight digits")
        # A reply the client rejects is sent again.
        client.send("qC")
        client.ack = False
        self.assertEqual(client.receive(), b"QC1")
        client.send_raw(b"-")
        self.assertEqual(client.receive(), b"QC1")
        client.send_raw(b"+")
        client.ack = True
        client.send("k")
        self.assert_exit(session, 2, "stopped")

    def test_no_ack_mode(self):
        session, client = self.start(self.tiny())
        self.assertEqual(client.ask("QStartNoAckMode"), "OK")
        client.ack = False
        self.assertEqual(client.ask("s"), "T05")
        self.assertEqual(client.reg(REG_PC), RAM + 4)
        self.assertEqual(client.ask("c"), "W00")
        self.assert_exit(session, 0, "done", "pass")

    def test_oversize_packet(self):
        """A payload longer than PacketSize gets `-` in ack mode, like a bad checksum, and a line on
        stderr; the next packet works."""
        session, client = self.start(self.tiny())
        client.send_raw(frame(b"m" * (PACKET_SIZE + 1)))
        self.assertEqual(client.byte(), ord("-"))
        self.assertEqual(client.ask("?"), "T05")
        client.send("k")
        _, stderr = self.assert_exit(session, 2, "stopped")
        self.assertIn("packet longer than PacketSize", stderr)

    def test_no_ack_mode_framing_errors(self):
        """In no-ack mode nothing is retransmitted, so nothing is dropped silently: an oversized
        packet gets E01 (and a stderr line), and a bad checksum is not verified, as the protocol
        allows there."""
        session, client = self.start(self.tiny())
        self.assertEqual(client.ask("QStartNoAckMode"), "OK")
        client.ack = False
        client.send_raw(frame(b"m" * (PACKET_SIZE + 1)))
        self.assertEqual(client.receive(), b"E01")
        client.send_raw(b"$?#00")
        self.assertEqual(client.receive(), b"T05")
        client.send_raw(b"$qC#zz")
        self.assertEqual(client.receive(), b"QC1")
        self.assertEqual(client.ask("c"), "W00")
        _, stderr = self.assert_exit(session, 0, "done", "pass")
        self.assertIn("packet longer than PacketSize", stderr)

    def test_listen_errors(self):
        """A port that is taken is an emulator error before the run; a bad port is refused."""
        image = self.tiny()
        taken = socket.socket()
        taken.bind(("127.0.0.1", 0))
        taken.listen(1)
        try:
            completed = subprocess.run(emulator_command(self.emulator, image) + ["--gdb", str(taken.getsockname()[1])], capture_output=True, text=True, timeout=TIMEOUT)
        finally:
            taken.close()
        self.assertEqual(completed.returncode, 2)
        self.assertIn("cannot listen", completed.stderr)
        completed = subprocess.run(emulator_command(self.emulator, image) + ["--gdb", "65536"],
                                   capture_output=True, text=True, timeout=TIMEOUT)
        self.assertEqual(completed.returncode, 2)
        self.assertIn("bad gdb port", completed.stderr)

    # A real gdb, when there is one.

    def test_real_gdb_session(self):
        """gdb-multiarch, riscv64-elf-gdb, or a multi-target gdb in batch mode: break on main, continue, read pc,
        stepi, examine the image's first words, and continue to the exit code."""
        self.need_selfcheck()
        gdb = shutil.which("gdb-multiarch") or shutil.which("riscv64-elf-gdb") or riscv_capable_gdb()
        if gdb is None:
            self.skipTest("neither gdb-multiarch, riscv64-elf-gdb, nor a gdb that knows riscv:rv32 is on PATH")
        if not SELFCHECK_ELF.exists():
            self.skipTest("build/rv32/selfcheck.elf is missing")
        process = subprocess.Popen(emulator_command(self.emulator, SELFCHECK) + ["--gdb", "0"],
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            line = process.stderr.readline().decode()
            port = int(LISTENING.match(line.strip()).group(1))
            words = SELFCHECK.read_bytes()[:16]
            first = [int.from_bytes(words[i:i + 4], "little") for i in range(0, 16, 4)]
            commands = ["set architecture riscv:rv32", f"target remote :{port}", "break *main",
                        "continue", "info registers pc", "stepi", "info registers pc",
                        "x/4wx 0x80000000", "continue"]
            completed = subprocess.run([gdb, "-nx", "-batch", *sum((["-ex", c] for c in commands), []),
                                        str(SELFCHECK_ELF)], capture_output=True, text=True, timeout=TIMEOUT)
            stdout, stderr = process.communicate(timeout=TIMEOUT)
        finally:
            if process.poll() is None:
                process.kill()
        out = completed.stdout + completed.stderr
        main = re.search(r"Breakpoint 1 at (0x[0-9a-f]+)", out)
        self.assertIsNotNone(main, out)
        pcs = re.findall(r"^pc\s+(0x[0-9a-f]+)", out, re.M)
        self.assertEqual(len(pcs), 2, out)
        self.assertEqual(int(pcs[0], 16), int(main.group(1), 16), out)
        self.assertEqual(int(pcs[1], 16), int(main.group(1), 16) + 4, out)
        self.assertIn("\t".join(f"0x{w:08x}" for w in first), out)
        self.assertRegex(out, r"exited normally|exited with code 0+\b")
        self.assertEqual(process.returncode, 0, stderr.decode())
        self.assertEqual(stdout.decode().strip(), SELFCHECK_CONSOLE)
        self.assertEqual(halt_line(stderr.decode())["halt"], "done")


if __name__ == "__main__":
    unittest.main()
