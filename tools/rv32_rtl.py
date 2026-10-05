#!/usr/bin/env python3
"""Run the RV32 RTL testbench and the emulator on one image and diff their traces.

The testbench (tests/rv32_tb.sv) prints the retirement trace in the emulator's
format, so agreement is a plain line-for-line comparison. This module holds the
pieces the tests and the Make targets share: compiling the testbench, running
either backend with the documented arguments, parsing the testbench's halt
line, deciding whether a run passed, and reporting the first trace difference
with context.
"""

import argparse
import re
from collections import namedtuple
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools.rv32_asm import PROGRAM_INPUTS, PROGRAMS, MTIME, SIMD_BASE, SIMD_COMMAND, SIMD_STATUS, SIMD_ENTRY, SIMD_CYCLES, SIMD_STALLS, SIMD_TRANSFERS, SIMD_INSTRUCTIONS, words_to_bytes, words_to_hex  # noqa: E402
from tools.rv32_asm import GPU_BASE, GPU_COMMAND, GPU_STATUS, GPU_ERROR, GPU_CYCLES, GPU_STALLS, GPU_READS, GPU_WRITES
from tools.rv32_image import write_hex  # noqa: E402
from tools.rv32_run_emu import DEFAULT_EMULATOR, emulator_command, halt_line, last_halt_line, parse_halt_line  # noqa: E402

RTL_SOURCES = [ROOT / "rtl" / "rv32" / name
               for name in ("rv32_fregfile.v", "rv32_fdecode.v", "rv32_regfile.v", "rv32_alu.v", "rv32_decode.v", "rv32_muldiv.v", "rv32.v",
                            "rv32_bus.v", "rv32_ram.v", "rv32_console.v", "rv32_done.v", "rv32_clint.v", "rv32_plic.v", "rv32_virtio_blk.v", "rv32_bootrom.v", "rv32_input.v", "rv32_display.v", "rv32_palette.v", "rv32_dma_window.v", "rv32_soc.v", "rv32_gpu.v", "rv32_g3d.v", "rv32_g3d_core.v", "rv32_simd4.v")]
RTL_SOURCES.extend([ROOT / "rtl/fp32/fp32.v", ROOT / "rtl/simd4/simd4.v"])
TESTBENCH = ROOT / "tests" / "rv32_tb.sv"
DEFAULT_SIMULATOR = "build/rv32/rv32_tb.vvp"
DEFAULT_OUT = "build/rv32/rtl"
BENCH_SEED = 7
COUNTERS = ("cycles", "steps", "stalls", "transfers")
# Issue #20's walk counters and issue #24's TLB counters: a run that translates prints all four.
TRANSLATION = ("walks", "ptw_waits", "tlb_hits", "tlb_misses")
DECIMAL = COUNTERS + ("cause", "fp_waits", "md_waits", "interrupts") + TRANSLATION
HEX = ("done", "tval", "pc", "word")
# The keys each halt reason carries besides the counters and the outcome (docs/rv32-rtl.md).
REQUIRED = {"done": ("done",), "double-fault": ("cause", "tval"), "limit": ()}
# A run's guest transcript and the simulator's own output are kept apart:
# `console` is what the guest printed (the emulator's stdout, or the file the
# testbench writes with +console), `noise` is anything the simulator itself
# printed on stdout (Icarus $fatal, $readmemh, and plusarg messages, Verilator
# %Error/%Fatal), which is empty for a clean run and always empty for the emulator.
# `checkpoints` is the `frame N <hash>` lines the backend wrote, when a file was asked for.
Run = namedtuple("Run", "status console noise stderr trace halt checkpoints", defaults=([],))


def compile_testbench(output, iverilog="iverilog", params=None):
    """Compile the testbench and the machine for Icarus into `output`; `params` overrides
    testbench parameters (`{"CONSOLE_BUSY": 2}`), the way `-G` does for a Verilator build."""
    overrides = [f"-Prv32_tb.{name}={value}" for name, value in (params or {}).items()]
    subprocess.run([iverilog, "-g2012", "-Wall", "-I" + str(ROOT / "rtl/fp32"), *overrides, "-s", "rv32_tb", "-o", str(output),
                    str(TESTBENCH), *map(str, RTL_SOURCES)], check=True)


def simulator_command(simulator, image, trace=None, console=None, wave=None, stall=None, seed=None, max_cycles=None,
                      checkpoints=None, input_script=None, reset_at=None, allow_lost_events=False, simd_stall=None, simd_seed=None, gpu_stall=None, gpu_seed=None,
                      ticks=None, console_input=None, disk=None, disk_out=None, console_prompt=None):
    """The command line for a compiled testbench: `vvp` for a .vvp file, else a Verilator binary."""
    simulator = Path(simulator)
    if simulator.suffix == ".vvp":
        command = ["vvp", str(simulator)]
    else:
        command = [str(simulator), "+verilator+quiet"]  # no simulation report on stdout
    command.append(f"+image={image}")
    if trace is not None:
        command.append(f"+trace={trace}")
    if console is not None:
        command.append(f"+console={console}")
    if wave is not None:
        command.append(f"+wave={wave}")
    if stall is not None:
        command.append(f"+stall={stall}")
    if seed is not None:
        command.append(f"+stall-seed={seed}")
    if max_cycles is not None:
        command.append(f"+max-cycles={max_cycles}")
    if checkpoints is not None:
        command.append(f"+checkpoints={checkpoints}")
    if input_script is not None:
        command.append(f"+input={input_script}")
    if reset_at is not None:
        command.append(f"+reset-at={reset_at}")
    if simd_stall is not None:
        command.append(f"+simd-stall={simd_stall}")
    if simd_seed is not None:
        command.append(f"+simd-seed={simd_seed}")
    if gpu_stall is not None: command.append(f"+gpu-stall={gpu_stall}")
    if gpu_seed is not None: command.append(f"+gpu-seed={gpu_seed}")
    if allow_lost_events:
        command.append("+allow-lost-events")
    if ticks is not None:
        command.append(f"+ticks={ticks}")
    if console_input is not None:
        command.append(f"+console-input={console_input}")
    if console_prompt is not None:  # hex, so spaces survive every simulator's plusarg parsing
        command.append(f"+console-prompt-hex={console_prompt.encode().hex()}")
    if disk is not None:
        command.append(f"+disk={disk}")
    if disk_out is not None:
        command.append(f"+disk-out={disk_out}")
    return command


def rtl_halt_line(stderr):
    """Parse the testbench's final `rv32_tb: halt=...` line into a dict, or None if absent.

    The halt reason decides which keys must be present (`done`; `cause` and
    `tval` of the undeliverable trap; none for a limit); anything else raises ValueError
    naming the line, so testbench format drift is caught here.
    """
    line = last_halt_line(stderr, "rv32_tb:")
    if line is None:
        return None
    fields = parse_halt_line(line, DECIMAL, HEX)
    reason = fields["halt"]
    if reason not in REQUIRED:
        raise ValueError(f"unknown halt reason {reason!r} in {line!r}")
    expected = {"halt", "outcome", *COUNTERS, *REQUIRED[reason]}
    for count in ("fp_waits", "md_waits", "interrupts", *TRANSLATION):
        if count in fields:
            expected.add(count)
            if fields[count] < 0: raise ValueError(f"negative {count} in {line!r}")
    present = [key in fields for key in TRANSLATION]
    if any(present) and not all(present):
        raise ValueError(f"{', '.join(TRANSLATION)} come together in {line!r}")
    if set(fields) != expected:
        raise ValueError(f"halt line keys {sorted(fields)} do not match {sorted(expected)} in {line!r}")
    return fields


def decode(data):
    """Guest and simulator output as text: any byte is allowed, so decoding never raises."""
    return data.decode("utf-8", errors="backslashreplace")


# The only thing a simulator may say on stdout in a clean run: Icarus announces
# the VCD it opened. The guest cannot reach stdout once +console is given, so
# this allowlist cannot hide guest text.
INFORMATIONAL = ("VCD info: ",)


def simulator_noise(stdout):
    """Simulator stdout with the known informational lines removed; empty for a clean run."""
    return "".join(line for line in stdout.splitlines(keepends=True) if not line.startswith(INFORMATIONAL))


def run_backend(command, trace, parse_halt, timeout, console=None, checkpoints=None):
    """Run one backend; the trace (and console) files are truncated first so a crash cannot pass.

    With `console`, the guest transcript is read from that file and the process's
    stdout is the simulator's own noise; without it the transcript is stdout and
    there is no noise channel (the emulator). A malformed halt line becomes
    `halt=None` with the reason appended to stderr. With `trace=None` the command
    writes no trace and the run's trace is empty.
    """
    if trace is not None:
        Path(trace).write_text("")
    if console is not None:
        Path(console).write_text("")
    if checkpoints is not None:
        Path(checkpoints).write_text("")
    try:
        completed = subprocess.run(command, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        partial = f" (a partial trace is in {trace})" if trace is not None else ""
        sys.exit(f"{command[0]} did not finish within {timeout} s; raise --timeout (--rtl-timeout for the simulator) or bound the run{partial}")
    stdout, stderr = decode(completed.stdout), decode(completed.stderr)
    try:
        halt = parse_halt(stderr)
    except ValueError as error:
        halt, stderr = None, f"{stderr}\n{error}\n"
    if console is None:
        transcript, noise = stdout, ""
    else:
        transcript, noise = decode(Path(console).read_bytes()), simulator_noise(stdout)
    lines = Path(checkpoints).read_text().splitlines() if checkpoints is not None else []
    retired = Path(trace).read_text().splitlines() if trace is not None else []
    return Run(completed.returncode, transcript, noise, stderr, retired, halt, lines)


def run_rtl(simulator, image_hex, trace, stall=None, seed=None, wave=None, max_cycles=None, timeout=120,
            checkpoints=None, input_script=None, reset_at=None, allow_lost_events=False, simd_stall=None, simd_seed=None, gpu_stall=None, gpu_seed=None,
            ticks=None, console_input=None, disk=None, console_prompt=None):
    """Run the testbench on a hex image with the documented plusargs; the console goes next to the
    trace, or next to the image (`<image>.console`) when `trace` is None and no trace is written."""
    if trace is not None:
        console = Path(trace).with_name(Path(trace).name + ".console")
    else:
        console = Path(image_hex).with_suffix(".console")
    disk_hex = disk_out = None
    if disk is not None:  # `disk` is a disk image the run reads and then holds the run's final disk
        disk_hex, disk_out = Path(f"{disk}.hex"), Path(f"{disk}.out.hex")
        write_hex(disk_hex, Path(disk).read_bytes())
        disk_out.unlink(missing_ok=True)
    command = simulator_command(simulator, image_hex, trace=trace, console=console, wave=wave,
                                stall=stall, seed=seed, max_cycles=max_cycles, checkpoints=checkpoints,
                                input_script=input_script, reset_at=reset_at, allow_lost_events=allow_lost_events,
                                simd_stall=simd_stall, simd_seed=simd_seed, gpu_stall=gpu_stall, gpu_seed=gpu_seed, ticks=ticks,
                                console_input=console_input, disk=disk_hex, disk_out=disk_out, console_prompt=console_prompt)
    run = run_backend(command, trace, rtl_halt_line, timeout, console=console, checkpoints=checkpoints)
    if disk is not None:
        # The testbench reads and writes the disk as hex words; the run's disk is the bytes again.
        # A run that never wrote it (a crash, a timeout) leaves no disk rather than an empty one.
        if not Path(disk_out).exists():
            Path(disk).unlink(missing_ok=True)
            return run
        text = Path(disk_out).read_text()
        # Icarus's $writememh adds `// 0x...` address comments; Verilator writes words only.
        words = [w for line in text.splitlines() if not line.lstrip().startswith("//") for w in line.split()]
        Path(disk).write_bytes(b"".join(int(w, 16).to_bytes(4, "little") for w in words))
    return run


def run_emulator(emulator, image_bin, trace, limit=None, timeout=120, checkpoints=None, input_script=None,
                 frames=None, allow_lost_events=False, console_input=None, disk=None, console_prompt=None):
    """Run the emulator on a flat image the same way tools/rv32_run_emu.py does, with a trace
    unless `trace` is None."""
    command = emulator_command(emulator, image_bin, trace=trace, limit=limit, checkpoints=checkpoints,
                               input_script=input_script, frames=frames, allow_lost_events=allow_lost_events,
                               console_input=console_input, disk=disk, console_prompt=console_prompt)
    return run_backend(command, trace, halt_line, timeout, checkpoints=checkpoints)


def diff_traces(rtl, emulator, context=3):
    """Return None if the traces are identical, else the first difference with preceding lines."""
    common = min(len(rtl), len(emulator))
    for index in range(common):
        if rtl[index] != emulator[index]:
            before = rtl[max(0, index - context):index]
            return (f"line {index + 1}: RTL '{rtl[index]}', emulator '{emulator[index]}'; "
                    f"preceding {before}")
    if len(rtl) != len(emulator):
        longer, count = ("emulator", len(emulator)) if len(emulator) > len(rtl) else ("RTL", len(rtl))
        return f"traces agree for {common} line(s), then the {longer} continues to {count}"
    return None


def describe(run):
    """Everything a failed run printed, for an error message."""
    return (f"status {run.status}\n--- simulator output ---\n{run.noise}--- guest console ---\n{run.console}"
            f"--- stderr ---\n{run.stderr}")


def check_passed(run, name="simulator"):
    """A matching trace is not enough: the backend must exit cleanly, print nothing of its own, and
    report `halt=done ... pass`; otherwise exit with everything it printed. A scripted event the
    backend dropped or never delivered is a nonzero exit status on both backends (docs/rv32.md,
    "Input"), so it is caught here without reading their messages."""
    if run.status != 0 or run.noise:
        sys.exit(f"{name} failed:\n{describe(run)}")
    if run.halt is None:
        sys.exit(f"{name} printed no valid halt line:\n{describe(run)}")
    if (run.halt["halt"], run.halt["outcome"]) != ("done", "pass"):
        sys.exit(f"{name} run did not pass: {run.halt}\n{describe(run)}")


def write_image(words, out, name):
    """Write both image forms and return (hex_path, bin_path)."""
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    hex_path, bin_path = out / f"{name}.hex", out / f"{name}.bin"
    hex_path.write_text(words_to_hex(words))
    bin_path.write_bytes(words_to_bytes(words))
    return hex_path, bin_path


def has_value_changes(vcd):
    """True when a VCD has its header closed and at least one timestamp after it: a dump that
    resolved to an empty scope has definitions and nothing else."""
    _, separator, changes = vcd.partition("$enddefinitions")
    return bool(separator) and any(line.startswith("#") for line in changes.splitlines())


def trap_records(trace):
    """The trace's trap lines without their step numbers: PC, word, cause, and value, in order. Device
    time moves the step numbers between backends; it never moves a fault or changes its cause.
    Interrupt entries (O1) are not trap lines: device time decides where they land."""
    return [line.split(" ", 1)[1] for line in trace if " trap " in line]


def trap_records_by_region(trace, faults_only=False):
    """The trap records grouped by the 128 KiB region of RAM (a program slot since O4) their PC lies
    in, each group in trace order. With preemption the order of two processes' exceptions depends
    on device time; the order of one process's own does not (docs/rv32-os.md, O4).

    Since issue #25 a process's PC is virtual. The kernel maps every process's slots at their own
    physical addresses, so the virtual PC is the physical one and its region still names the
    process; satp would not, since a process table entry, and so a page table, outlives the
    process, and which entry a background job's successor gets depends on device time. A kernel
    that put two processes at one virtual address would need the physical PC in the trap record."""
    claims = claim_lines(trace) if faults_only else set()
    groups = {}
    for index, line in enumerate(trace):
        if " trap " not in line:
            continue
        record = line.split(" ", 1)[1]
        fields = record.split()
        if faults_only and fields[3] in ("8", "11"):
            continue  # an environment call: a kernel may run it again when it had to wait
        if index in claims:
            continue  # issue #33: a first F instruction since another process took the FPU, run again
        groups.setdefault(int(fields[0], 16) >> 17, []).append(record)
    return groups


def claim_lines(trace):
    """The indices of the trace's FPU claims (issue #33): illegal-instruction traps on an F
    instruction (floating_word) whose PC is the next one its 128 KiB region runs, so the kernel ran
    it again; where those land depends on device time. A trap the process does not come back from,
    such as an invalid F instruction it is killed for, is not one, and is compared."""
    claims, pending = set(), {}  # region -> (index, pc) of a trap waiting for its region's next line
    for index, line in enumerate(trace):
        fields = line.split()
        if len(fields) < 3:
            continue
        region = int(fields[1], 16) >> 17
        if region in pending:
            trap_index, pc = pending.pop(region)
            if fields[1] == pc:  # run again, whether it then retires or traps for good
                claims.add(trap_index)
        if len(fields) >= 5 and fields[3] == "trap" and fields[4] == "2" and floating_word(int(fields[2], 16)):
            pending[region] = (index, fields[1])
    return claims


def floating_word(word):
    """Whether `word` is an F instruction (FLW, FSW, the arithmetic opcodes) or a CSR instruction on
    fflags, frm or fcsr: what mstatus.FS Off makes illegal (issue #33). The OS's lazy switch turns FS
    Off for every process but the FPU's owner, so where such a trap lands depends on device time.
    The same test as the kernel's floating_instruction() and the RTL's fs_illegal (rv32.v)."""
    opcode, funct3, csr = word & 0x7F, (word >> 12) & 7, word >> 20
    return opcode in (0x07, 0x27, 0x43, 0x47, 0x4B, 0x4F, 0x53) or (opcode == 0x73 and funct3 & 3 and 1 <= csr <= 3)


def takes_interrupts(trace):
    """Whether a trace has an interrupt entry (O1): with cycle ticks it lands on a
    backend-dependent instruction, so only step ticks keep trace comparison."""
    return any(" interrupt " in line for line in trace)


from tools import rv32_asm as asm  # noqa: E402

SIMD_ACCESS = re.compile(r"mem\[(?:" + "|".join(f"{SIMD_BASE+offset:08x}" for offset in
    (SIMD_COMMAND, SIMD_STATUS, SIMD_ENTRY, SIMD_CYCLES, SIMD_STALLS, SIMD_TRANSFERS, SIMD_INSTRUCTIONS)) + r")\](?:->|<-)")

GPU_ACCESS = re.compile(r"mem\[(?:" + "|".join(f"{GPU_BASE+offset:08x}" for offset in
    (GPU_COMMAND, GPU_STATUS, GPU_ERROR, GPU_CYCLES, GPU_STALLS, GPU_READS, GPU_WRITES)) + r")\](?:->|<-)")

G3D_ACCESS = re.compile(r"mem\[(?:" + "|".join(f"{asm.G3D_BASE+offset:08x}" for offset in
    (asm.G3D_COMMAND, asm.G3D_STATUS, asm.G3D_ERROR, asm.G3D_FAULT_PC, asm.G3D_CYCLES, asm.G3D_STALLS,
     asm.G3D_INSTRUCTIONS, asm.G3D_TRANSFERS, asm.G3D_DIVIDES, asm.G3D_PIXELS, asm.G3D_ZFAIL, asm.G3D_CULLED))
    + r")\](?:->|<-)")

# The Zicntr counters that count device ticks: cycle, time and their high halves. `instret`
# counts retirements, which agree on every backend, so reading it keeps trace comparison.
DEVICE_TIME_CSRS = (asm.CYCLE, asm.TIME, asm.CYCLEH, asm.TIMEH)


def reads_device_time(trace):
    """Whether a trace reads device time: a load from either word of the CLINT's mtime, or a CSR
    instruction on the cycle or time counter (docs/rv32.md, "Device time")."""
    for line in trace:
        if f"mem[{MTIME:08x}]->" in line or f"mem[{MTIME + 4:08x}]->" in line:
            return True
        parts = line.split(" ", 3)
        if len(parts) >= 3 and " trap " not in line:  # a trapped counter access read nothing
            word = int(parts[2], 16)
            if word & 0x7F == 0x73 and (word >> 12) & 3 and word >> 20 in DEVICE_TIME_CSRS:
                return True
    return False


def uses_accelerator(trace):
    """Only successful register accesses justify asynchronous result comparison."""
    return any(SIMD_ACCESS.search(line) or GPU_ACCESS.search(line) or G3D_ACCESS.search(line) for line in trace)


def store_records(trace):
    """Ordered stores including PC, instruction, address, data and width, without step numbers."""
    return [line.split(" ", 1)[1] for line in trace if "<-" in line]


def compare_backends(rtl, emulator, compare, compare_stores=False, checkpoints="lines", traps="all"):
    """What must agree between two passing runs, as the first mismatch or None: the console
    transcript and the checkpoint lines always, and in outputs mode nothing more, since no trace was
    written (issue #35); the whole retirement trace in trace mode; in
    results mode (device time) the trap records, which step numbers never move, compared per 128 KiB
    region of their PC: one process's order is fixed, two processes' interleaving is not (O4).
    compare_stores is an opt-in for firmware whose stores are timing-independent;
    even timer-free accelerator polling may store a varying poll count."""
    if rtl.console != emulator.console:
        return f"console mismatch: RTL {rtl.console!r}, emulator {emulator.console!r}"
    if checkpoints == "count":  # frames drawn by programs the scheduler interleaves (O4)
        if len(rtl.checkpoints) != len(emulator.checkpoints):
            return f"checkpoint count mismatch: RTL {len(rtl.checkpoints)}, emulator {len(emulator.checkpoints)}"
    elif rtl.checkpoints != emulator.checkpoints:
        return f"checkpoint mismatch: RTL {rtl.checkpoints}, emulator {emulator.checkpoints}"
    if compare == "outputs":  # no traces were written (issue #35): what the guest printed and presented is all
        return None
    if compare == "results":
        faults_only = traps == "faults"
        rtl_traps = trap_records_by_region(rtl.trace, faults_only)
        emulator_traps = trap_records_by_region(emulator.trace, faults_only)
        if rtl_traps != emulator_traps:
            region = next(r for r in sorted(set(rtl_traps) | set(emulator_traps)) if rtl_traps.get(r) != emulator_traps.get(r))
            return (f"trap mismatch in the region at {region << 17:#010x}: RTL {rtl_traps.get(region, [])}, "
                    f"emulator {emulator_traps.get(region, [])}")
        if compare_stores:
            difference = diff_traces(store_records(rtl.trace), store_records(emulator.trace))
            if difference:
                return f"store mismatch: {difference}"
        return None
    difference = diff_traces(rtl.trace, emulator.trace)
    return f"trace mismatch: {difference}" if difference else None


def generated_paths(out, name, frames=None):
    """Every file a run may write or truncate under `out` (and the frames directory), so an
    input script that names one of them can be refused before anything is written."""
    paths = {out / f"{name}.{suffix}" for suffix in ("hex", "bin", "input", "vcd", "emu.trace", "emu.checkpoints",
                                                     "rtl.trace", "rtl.trace.console", "rtl.checkpoints", "emu.disk",
                                                     "rtl.disk", "rtl.disk.hex", "rtl.disk.out.hex",
                                                     "console")}  # the untraced RTL run's console (issue #35)
    if frames is not None:
        paths |= set(frames.glob("frame-*.ppm"))
    return paths


def refuse_aliased_input(script, out, name, frames=None, option="--input"):
    """Exit if `script` is (by path or by inode) one of the run's own files: the trace, console, and
    checkpoint files are truncated before a backend starts, which would destroy the script (or make
    an expected-checkpoints file compare the run against itself)."""
    for path in generated_paths(out, name, frames):
        same = path.resolve() == script.resolve() or (path.exists() and script.exists() and path.samefile(script))
        if same:
            sys.exit(f"{option} {script} names a file this run writes ({path}); keep it outside --out")


def read_expected_checkpoints(path):
    """The `frame N <hash>` lines of an expected-checkpoints file; blank lines and `#` comments are
    skipped. A file with no lines, or a malformed one, is refused: an empty expectation would compare
    nothing and pass."""
    if not path.is_file():
        sys.exit(f"--expect-checkpoints {path} is not a file")
    lines = [line.strip() for line in path.read_text().splitlines()]
    lines = [line for line in lines if line and not line.startswith("#")]
    if not lines:
        sys.exit(f"--expect-checkpoints {path} has no `frame N <hash>` lines")
    for number, line in enumerate(lines, 1):
        if not re.fullmatch(r"frame \d+ [0-9a-f]{8}", line):
            sys.exit(f"--expect-checkpoints {path}: line {number} is not `frame N <hash>`: {line!r}")
    return lines


def first_checkpoint_difference(observed, expected):
    """Where two checkpoint lists first differ, for a message that does not print 200 lines."""
    for index, (got, want) in enumerate(zip(observed, expected), 1):
        if got != want:
            return f"line {index}: got {got!r}, expected {want!r}"
    return f"{len(observed)} line(s) observed, {len(expected)} expected"


def cycle_relation(rtl):
    """Relate the testbench's cycle count to the trace: 4 cycles per instruction plus one per data
    access (a load or store has one, an AMO two since issue #34, a failed SC.W none), plus the
    stalls. With Sv32 on, the walk's cycles are one per TLB miss and one per page-table read (issue
    #24: a hit costs none). Returns (text, holds); `holds` is None when a trap line makes the
    formula inapplicable (a trap costs the cycles up to the state that raised it)."""
    steps = len(rtl.trace)
    memory = sum(line.count("mem[") for line in rtl.trace)
    traps = sum(" trap " in line or " interrupt " in line for line in rtl.trace)
    halt = rtl.halt
    expected = (4 * steps + memory + halt["stalls"] + halt.get("fp_waits", 0) + halt.get("md_waits", 0) +
                halt.get("ptw_waits", 0))
    walks = halt.get("walks", 0)
    text = (f"cycles {halt['cycles']} = 4 x {steps} + {memory} data + {halt['stalls']} stalls; "
            f"transfers {halt['transfers']} = {steps} fetches + {memory} data")
    if halt.get("fp_waits", 0):
        text += f"; plus {halt['fp_waits']} FPU issue/wait cycles"
    if halt.get("md_waits", 0):
        text += f"; plus {halt['md_waits']} multiply/divide wait cycles"
    if walks:
        text += (f"; plus {halt['ptw_waits']} page-table walk cycles = {halt['tlb_misses']} TLB misses + "
                 f"{walks} page-table reads ({halt['tlb_hits']} TLB hits)")
    if traps:
        return f"{text} (not exact: {traps} trap lines)", None
    broken = [name for name, kept in (
        ("cycles", halt["cycles"] == expected),
        ("transfers", halt["transfers"] == steps + memory + walks),
        ("ptw_waits = tlb_misses + walks", halt.get("ptw_waits", 0) == halt.get("tlb_misses", 0) + walks),
        # Every lookup belongs to a fetch or a data access; hits cost no cycle, so this is their only check.
        ("tlb_hits + tlb_misses <= fetches + data", halt.get("tlb_hits", 0) + halt.get("tlb_misses", 0) <= steps + memory),
    ) if not kept]
    if broken:
        text += f" (does not hold: {'; '.join(broken)})"
    return text, not broken


def check_fp_waits(rtl, expected):
    """A workload-specific latency pin, independent of the accounting identity."""
    if expected is not None and rtl.halt.get("fp_waits", 0) != expected:
        sys.exit(f"FPU wait cycles: got {rtl.halt.get('fp_waits', 0)}, expected {expected}")


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--mode", choices=("check", "waves", "bench"), default="check")
    parser.add_argument("--program", choices=sorted(PROGRAMS), default="loop", help="assembled program to run")
    parser.add_argument("--image", type=Path, help="a flat .bin image to run instead of an assembled program")
    console = parser.add_mutually_exclusive_group()
    console.add_argument("--expect-console", help="the guest console output both backends must produce")
    console.add_argument("--expect-console-file", type=Path, help="the same, read from a file")
    parser.add_argument("--expect-last-line", help="the last console line both backends must produce")
    parser.add_argument("--expect-checkpoint", action="append", default=[], metavar="LINE",
                        help="the `frame N <hash>` lines both backends must write, exactly and in order (repeatable)")
    parser.add_argument("--expect-checkpoints", type=Path, metavar="FILE",
                        help="a file of `frame N <hash>` lines to expect, one per line, after any --expect-checkpoint")
    parser.add_argument("--allow-lost-events", action="store_true",
                        help="tell both backends to accept a run that dropped a scripted event or never delivered one")
    parser.add_argument("--input", type=Path,
                        help="input script delivered to both backends (docs/rv32.md, Input); not a file under --out")
    parser.add_argument("--frames", type=Path, help="directory for the emulator's frame-NNNN.ppm pictures")
    parser.add_argument("--console-input", type=Path,
                        help="bytes both backends' consoles receive, all waiting from reset (O2); not a file under --out")
    parser.add_argument("--console-prompt",
                        help="make line k of --console-input visible only once the guest has sent this k times (issue #36)")
    parser.add_argument("--limit", type=int, help="the emulator's instruction limit (default: its own)")
    parser.add_argument("--disk", type=Path, help="the virtio-blk disk both backends start from (O3); never written")
    parser.add_argument("--disk-out", type=Path, help="where the emulator's final disk goes, after the RTL's is found equal")
    gpu_delay = parser.add_mutually_exclusive_group()
    gpu_delay.add_argument("--gpu-stall",type=int,help="fixed waits per graphics memory transfer")
    gpu_delay.add_argument("--gpu-seed",type=int,help="seeded 0..3 graphics memory waits")
    simd_delay = parser.add_mutually_exclusive_group()
    simd_delay.add_argument("--simd-stall", type=int, help="fixed wait cycles per accelerator data transfer")
    simd_delay.add_argument("--simd-seed", type=int, help="seeded 0..3 waits per accelerator data transfer")
    parser.add_argument("--compare", choices=("trace", "results", "outputs"), default="trace",
                        help="`trace`: identical retirement traces and the cycle formula; `results`: identical "
                             "console, outcome, and checkpoints, for a program that reads the timer, the cycle/time counters or accelerator registers (device time); "
                             "`outputs`: the same as `results` less the trap records and the device-time check, with no trace written, only for a run too long to trace (issue #35: Doom)")
    parser.add_argument("--compare-checkpoints", choices=("lines", "count"), default="lines",
                        help="`count` compares only how many frames each backend presented, for programs whose frames "
                             "depend on how the scheduler interleaved them (O4); results mode only")
    parser.add_argument("--compare-traps", choices=("all", "faults"), default="all",
                        help="`faults` leaves environment calls out of the compared trap records, for a kernel that "
                             "retries a blocked system call (O2) under preemption (O4), and the illegal-instruction "
                             "traps of F instructions its lazy FPU switch runs again (issue #33); results mode only")
    parser.add_argument("--compare-stores", action="store_true",
                        help="also compare ordered stores in results mode; firmware must have timing-independent stores")
    parser.add_argument("--ticks", choices=("cycles", "steps"), default="cycles",
                        help="the RTL's device tick: `cycles` (the default) or `steps`, the deterministic mode in which mtime "
                             "and cycle advance once per step as on the emulator, so timer reads and interrupts keep trace comparison")
    parser.add_argument("--backend", choices=("both", "emulator"), default="both",
                        help="`emulator` runs and checks the emulator only")
    parser.add_argument("--allow-traps", action="store_true",
                        help="accept a trace with trap lines, where the cycle formula is not exact")
    parser.add_argument("--emulator", default=DEFAULT_EMULATOR)
    parser.add_argument("--simulator", help=f".vvp file or Verilator binary (default {DEFAULT_SIMULATOR}; unused with --backend emulator)")
    parser.add_argument("--out", default=DEFAULT_OUT, help="directory for the image, traces, and VCD")
    parser.add_argument("--timeout", type=float, default=120.0, help="seconds each backend may run (default 120)")
    parser.add_argument("--rtl-timeout", type=float,
                        help="seconds the RTL simulator may run, overriding --timeout for it alone (a slow simulator)")
    parser.add_argument("--max-cycles", type=int, help="RTL cycle budget (default: testbench budget of 10000000)")
    parser.add_argument("--stall", type=int, default=None, help="fixed stall cycles per request")
    parser.add_argument("--seed", type=int, default=None, help="random 0..3 stall cycles per request")
    parser.add_argument("--expect-fp-waits", type=int, help="pin total RTL FPU issue/wait cycles")
    args = parser.parse_args()
    if args.compare_stores and args.compare != "results":
        parser.error("--compare-stores requires --compare results")
    if args.compare_traps == "faults" and args.compare != "results":
        parser.error("--compare-traps faults requires --compare results")
    if args.compare_checkpoints == "count" and args.compare not in ("results", "outputs"):
        parser.error("--compare-checkpoints count requires --compare results or outputs")
    for option in ("simd_stall", "simd_seed", "gpu_stall", "gpu_seed"):
        value = getattr(args, option)
        if value is not None and not 0 <= value <= 2147483647:
            parser.error(f"--{option.replace('_', '-')} must be in 0..2147483647")
        if value is not None and args.backend == "emulator":
            parser.error(f"--{option.replace('_', '-')} requires the RTL backend")
    if args.expect_fp_waits is not None:
        if args.expect_fp_waits < 0: parser.error("--expect-fp-waits must not be negative")
        if args.backend == "emulator": parser.error("--expect-fp-waits requires the RTL backend")
    if args.stall is not None and args.stall < 0:
        parser.error("--stall must not be negative")
    if args.stall is not None and args.seed is not None:
        parser.error("--stall and --seed are exclusive")
    if args.max_cycles is not None and not 1 <= args.max_cycles <= 2147483647:
        parser.error("--max-cycles must be in 1..2147483647")
    if args.timeout <= 0:
        parser.error("--timeout must be positive")
    if args.rtl_timeout is not None and args.rtl_timeout <= 0:
        parser.error("--rtl-timeout must be positive")
    if args.simulator is None and args.backend == "both":
        args.simulator = DEFAULT_SIMULATOR
    for path in (args.emulator, args.simulator):
        if path is not None and not Path(path).exists() and shutil.which(path) is None:
            parser.error(f"{path} does not exist; run make build-rv32-emu / build-rv32-rtl first")

    out = Path(args.out)
    name = args.image.stem if args.image is not None else args.program
    if args.input is not None:
        refuse_aliased_input(args.input, out, name, args.frames)
    if args.console_input is not None:
        refuse_aliased_input(args.console_input, out, name, args.frames, option="--console-input")
    if args.disk is not None:
        refuse_aliased_input(args.disk, out, name, args.frames, option="--disk")
    if args.expect_checkpoints is not None:
        if not args.expect_checkpoints.exists():
            parser.error(f"{args.expect_checkpoints} does not exist")
        refuse_aliased_input(args.expect_checkpoints, out, name, args.frames, option="--expect-checkpoints")
        args.expect_checkpoint += read_expected_checkpoints(args.expect_checkpoints)  # never empty
    if args.image is not None:
        if not args.image.exists():
            parser.error(f"{args.image} does not exist; run make check-rv32-image first")
        out.mkdir(parents=True, exist_ok=True)
        hex_path, bin_path = out / f"{name}.hex", args.image
        write_hex(hex_path, bin_path.read_bytes())
    else:
        hex_path, bin_path = write_image(PROGRAMS[name](), out, name)
        if args.input is None and name in PROGRAM_INPUTS:
            args.input = out / f"{name}.input"
            args.input.write_text(PROGRAM_INPUTS[name])
    if args.frames is not None:
        args.frames.mkdir(parents=True, exist_ok=True)
        for stale in args.frames.glob("frame-*.ppm"): # a previous run's frames must not survive this one
            stale.unlink()
    def backend_disk(backend):
        """A fresh copy of --disk for one backend's run, or None."""
        if args.disk is None:
            return None
        copy = out / f"{name}.{backend}.disk"
        copy.write_bytes(args.disk.read_bytes())
        return copy

    # Outputs mode writes no trace, so it cannot tell whether a trace comparison would have been
    # possible (the device-time checks below see an empty trace): it is for runs too long to trace,
    # and says what it leaves out.
    untraced = args.compare == "outputs"

    def trace_path(backend):
        return None if untraced else out / f"{name}.{backend}.trace"

    emulator = run_emulator(args.emulator, bin_path, trace_path("emu"), timeout=args.timeout,
                            checkpoints=out / f"{name}.emu.checkpoints", input_script=args.input, frames=args.frames,
                            allow_lost_events=args.allow_lost_events, console_input=args.console_input, limit=args.limit,
                            disk=backend_disk("emu"), console_prompt=args.console_prompt)
    check_passed(emulator, "emulator")
    # Device time differs for timers and asynchronous accelerators. Compare guest
    # results and trap records when either interface makes the CPU trace timing-dependent.
    # In step-tick mode the RTL counts device time as the emulator does, so timer reads and interrupts
    # stay trace-comparable; accelerators still run per clock.
    reads_timer = reads_device_time(emulator.trace) and args.ticks == "cycles"
    interrupted = takes_interrupts(emulator.trace) and args.ticks == "cycles"
    if args.compare == "trace" and reads_timer:
        sys.exit("this program reads the timer or the cycle/time counters, so its traces differ by design; use --compare results or --ticks steps")
    if args.compare == "trace" and interrupted:
        sys.exit("this program takes interrupts, which land on backend-dependent instructions; use --compare results or --ticks steps")
    uses_simd = uses_accelerator(emulator.trace)
    if args.compare == "trace" and uses_simd:
        sys.exit("this program accesses accelerator registers; use --compare results")
    if args.compare == "results" and not (reads_timer or uses_simd or interrupted):
        sys.exit("--compare results is for a program that reads the timer, the cycle/time counters or accelerator registers, "
                 "or takes interrupts, with cycle ticks; this one does not, use --compare trace")
    if args.expect_console_file is not None:
        args.expect_console = args.expect_console_file.read_bytes().decode().rstrip("\n")  # bytes: keep a guest's \r (Linux sends \r\n)
    if args.expect_console is not None and emulator.console.rstrip("\n") != args.expect_console:
        sys.exit(f"emulator console {emulator.console!r} is not {args.expect_console!r}")
    last_line = emulator.console.rstrip("\n").rsplit("\n", 1)[-1]
    if args.expect_last_line is not None and last_line != args.expect_last_line:
        sys.exit(f"emulator's last console line {last_line!r} is not {args.expect_last_line!r}")
    if args.expect_checkpoint and emulator.checkpoints != args.expect_checkpoint:
        sys.exit(f"emulator checkpoints are not the expected ones: {first_checkpoint_difference(emulator.checkpoints, args.expect_checkpoint)}")
    if args.backend == "emulator":
        if args.disk_out is not None:
            args.disk_out.write_bytes((out / f"{name}.emu.disk").read_bytes())
        print(emulator.stderr.strip().splitlines()[-1])
        extent = f"{emulator.halt['steps']} steps, untraced" if untraced else f"{len(emulator.trace)} trace lines"
        print(f"emulator: {extent}, {len(emulator.checkpoints)} checkpoint(s), console ends {last_line!r}")
        return

    def run_rtl_as_asked(stall, seed, wave=None):
        """The RTL run every mode makes: the arguments given, with this stall setting."""
        return run_rtl(args.simulator, hex_path, trace_path("rtl"), stall=stall, seed=seed, wave=wave,
                       timeout=args.timeout if args.rtl_timeout is None else args.rtl_timeout, max_cycles=args.max_cycles, checkpoints=out / f"{name}.rtl.checkpoints",
                       input_script=args.input, allow_lost_events=args.allow_lost_events, simd_stall=args.simd_stall,
                       simd_seed=args.simd_seed, gpu_stall=args.gpu_stall, gpu_seed=args.gpu_seed, ticks=args.ticks,
                       console_input=args.console_input, disk=backend_disk("rtl"), console_prompt=args.console_prompt)

    def compare_disks():
        """What each backend left on its disk (O3): identical, or the run fails."""
        emu_path, rtl_path = out / f"{name}.emu.disk", out / f"{name}.rtl.disk"
        if not rtl_path.exists():
            sys.exit("the RTL wrote no disk (+disk-out): it did not finish")
        emu_disk, rtl_disk = emu_path.read_bytes(), rtl_path.read_bytes()
        if emu_disk != rtl_disk:
            differs = next((i for i in range(min(len(emu_disk), len(rtl_disk))) if emu_disk[i] != rtl_disk[i]), None)
            sys.exit(f"disk mismatch: {len(rtl_disk)} bytes on the RTL, {len(emu_disk)} on the emulator, first difference at {differs}")
        return emu_disk

    if args.mode == "bench":
        print("stall  cycles  stalls  transfers  steps")
        seed = BENCH_SEED if args.seed is None else args.seed
        for stall, seed in [(0, None), (1, None), (2, None), (3, None), (None, seed)]:
            rtl = run_rtl_as_asked(stall, seed)
            check_passed(rtl)
            check_fp_waits(rtl, args.expect_fp_waits)
            mismatch = compare_backends(rtl, emulator, args.compare, compare_stores=args.compare_stores,
                                        checkpoints=args.compare_checkpoints, traps=args.compare_traps)  # the same agreement as a check run
            if mismatch:
                sys.exit(f"stall={stall} seed={seed}: {mismatch}")
            if args.disk is not None:
                compare_disks()
            if stall is not None and rtl.halt["stalls"] != stall * rtl.halt["transfers"]:
                sys.exit(f"stall={stall}: {rtl.halt['stalls']} stalls for {rtl.halt['transfers']} transfers")
            label = f"seed {seed}" if seed is not None else str(stall)
            print(f"{label:>6}  {rtl.halt['cycles']:>6}  {rtl.halt['stalls']:>6}  "
                  f"{rtl.halt['transfers']:>9}  {rtl.halt['steps']:>5}")
        return

    # Random stalls replace the fixed count; waves default to a stall so the handshake is visible.
    if args.seed is not None:
        stall = None
    elif args.stall is not None:
        stall = args.stall
    elif args.mode == "waves":
        stall = 2
    else:
        stall = 0
    wave = out / f"{name}.vcd" if args.mode == "waves" else None
    rtl = run_rtl_as_asked(stall, args.seed, wave)
    print(emulator.stderr.strip().splitlines()[-1])
    print(rtl.stderr.strip().splitlines()[-1] if rtl.stderr.strip() else "rv32_tb: no halt line")
    check_passed(rtl)
    check_fp_waits(rtl, args.expect_fp_waits)
    mismatch = compare_backends(rtl, emulator, args.compare, compare_stores=args.compare_stores,
                                        checkpoints=args.compare_checkpoints, traps=args.compare_traps)
    if mismatch:
        sys.exit(mismatch)
    if args.disk is not None:
        emu_disk = compare_disks()
        print(f"disks identical: {len(emu_disk)} bytes")
        if args.disk_out is not None:
            args.disk_out.write_bytes(emu_disk)
    if untraced:
        print(f"outputs identical (untraced): {len(emulator.console.splitlines())} console line(s) ending {last_line!r}, "
              f"{len(rtl.checkpoints)} checkpoint(s); RTL {rtl.halt['steps']} instructions in {rtl.halt['cycles']} cycles, "
              f"emulator {emulator.halt['steps']} instructions; no trace, so no trap records or stores compared")
    elif args.compare == "results":
        # What the guest printed and presented agreed, and so did every fault it took: the same PC,
        # word, cause, and value in the same order, only the step numbers differing.
        if args.compare_stores:
            print(f"stores identical: {len(store_records(rtl.trace))} ordered records (step numbers excluded)")
        traps = len(trap_records(rtl.trace))
        print(f"results identical: {len(emulator.console.splitlines())} console line(s) ending {last_line!r}, "
              f"{len(rtl.checkpoints)} checkpoint(s) {rtl.checkpoints}, {traps} trap(s) alike; RTL "
              f"{len(rtl.trace)} instructions in {rtl.halt['cycles']} cycles, emulator {len(emulator.trace)} instructions")
    else:
        print(f"traces identical: {len(rtl.trace)} lines; {out / f'{name}.rtl.trace'}")
        relation, holds = cycle_relation(rtl)
        print(relation)
        if holds is None and not args.allow_traps:
            sys.exit("the trace has trap lines, so the cycle formula cannot be checked; pass --allow-traps if that is expected")
        if holds is False:
            sys.exit("the cycle count does not follow the state machine")
    if wave is not None:
        if not wave.exists() or not has_value_changes(wave.read_text()):
            sys.exit(f"the simulator wrote no waveform to {wave}")
        print(f"waveform: {wave}")


if __name__ == "__main__":
    main()
