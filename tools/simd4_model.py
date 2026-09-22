"""SIMD4 word encoders and an instruction-level reference (no cycle model)."""

from dataclasses import dataclass


HLT, LDI, LANE, ADD, ADDI, LOAD, STORE, SETLOOP, LOOP = range(9)
MUL, MAC, MACU, CLRA, RDA = range(9, 14)
LAST_OPCODE = RDA


def word(op, rd=0, ra=0, rb=0, imm=0):
    if not (0 <= op <= 255 and all(0 <= r < 4 for r in (rd, ra, rb)) and 0 <= imm <= 65535):
        raise ValueError("opcode/register/immediate out of range")
    return (op << 24) | (rd << 22) | (ra << 20) | (rb << 18) | imm


def signed16(value):
    return value - 65536 if value & 0x8000 else value


def snapshot(registers, accumulators, pc, loop):
    """Pack lane registers, accumulators, PC and loop count the way the RTL exposes them."""
    lanes = len(registers)
    flat = sum(value << (16 * (lane * 4 + reg))
               for lane, row in enumerate(registers) for reg, value in enumerate(row))
    flat |= sum(acc << (lanes * 64 + 32 * lane) for lane, acc in enumerate(accumulators))
    return flat | (pc << (lanes * 96)) | (loop << (lanes * 96 + 8))


@dataclass
class Execution:
    memory: list
    retirements: list
    transfers: list
    final_state: int
    attempts: int
    fault: bool

    @property
    def base_cycles(self):
        return 2 * self.attempts + len(self.transfers)


def execute(program, initial_memory, lanes=4, entry=0, limit=4096):
    if lanes not in (1, 2, 4) or len(program) != 256 or len(initial_memory) != 256 or not 0 <= entry < 256:
        raise ValueError("expected supported lane count, two 256-word images, and byte entry")
    if any(not 0 <= n <= 0xffffffff for n in program) or any(not 0 <= n <= 0xffff for n in initial_memory):
        raise ValueError("image word out of range")
    registers = [[0] * 4 for _ in range(lanes)]
    accumulators = [0] * lanes
    memory = initial_memory.copy()
    pc, loop = entry, 0
    retirements, transfers = [], []
    for attempt in range(1, limit + 1):
        address, instruction = pc, program[pc]
        pc = (pc + 1) % 256
        op, rd, ra, rb, immediate = (instruction >> 24, (instruction >> 22) & 3,
                                    (instruction >> 20) & 3, (instruction >> 18) & 3,
                                    instruction & 0xffff)
        fault = op > LAST_OPCODE or (op == SETLOOP and immediate == 0) or (op == LOOP and loop == 0)
        if fault:
            return Execution(memory, retirements, transfers,
                             snapshot(registers, accumulators, pc, loop), attempt, True)
        if op == SETLOOP:
            loop = immediate
        elif op == LOOP:
            loop -= 1
            if loop:
                pc = immediate % 256
        elif op:
            for lane, row in enumerate(registers):
                if op == LDI:
                    row[rd] = immediate
                elif op == LANE:
                    row[rd] = lane
                elif op == ADD:
                    row[rd] = (row[ra] + row[rb]) % 65536
                elif op == ADDI:
                    row[rd] = (row[ra] + immediate) % 65536
                elif op in (LOAD, STORE):
                    location = (row[ra] + immediate) % 256
                    data = memory[location] if op == LOAD else row[rd]
                    transfers.append(((op == STORE) << 24) | (location << 16) | data)
                    if op == LOAD:
                        row[rd] = data
                    else:
                        memory[location] = data
                elif op == MUL:
                    row[rd] = (row[ra] * row[rb]) % 65536
                elif op == MAC:
                    accumulators[lane] = (accumulators[lane] + signed16(row[ra]) * signed16(row[rb])) % 2**32
                elif op == MACU:
                    accumulators[lane] = (accumulators[lane] + row[ra] * row[rb]) % 2**32
                elif op == CLRA:
                    accumulators[lane] = 0
                elif op == RDA:
                    acc = accumulators[lane]
                    signed_acc = acc - 2**32 if acc & 0x80000000 else acc
                    row[rd] = (signed_acc >> (immediate & 31)) % 65536
        state = snapshot(registers, accumulators, pc, loop)
        retirements.append(state | (instruction << (lanes * 96 + 24)) |
                           (address << (lanes * 96 + 56)))
        if op == HLT:
            return Execution(memory, retirements, transfers, state, attempt, False)
    raise ValueError("reference program exceeded instruction limit")


def image(words):
    if len(words) > 256:
        raise ValueError("program exceeds 256 words")
    return words + [0] * (256 - len(words))
