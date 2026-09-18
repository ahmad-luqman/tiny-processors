"""SIMD4 word encoders and an instruction-level reference (no cycle model)."""

from dataclasses import dataclass


def word(op, rd=0, ra=0, rb=0, imm=0):
    if not (0 <= op <= 255 and all(0 <= r < 4 for r in (rd, ra, rb)) and 0 <= imm <= 65535):
        raise ValueError("opcode/register/immediate out of range")
    return (op << 24) | (rd << 22) | (ra << 20) | (rb << 18) | imm


def snapshot(registers, pc, loop):
    flat = sum(value << (16 * (lane * 4 + reg))
               for lane, row in enumerate(registers) for reg, value in enumerate(row))
    return flat | (pc << (len(registers) * 64)) | (loop << (len(registers) * 64 + 8))


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
    memory = initial_memory.copy()
    pc, loop = entry, 0
    retirements, transfers = [], []
    for attempt in range(1, limit + 1):
        address, instruction = pc, program[pc]
        pc = (pc + 1) % 256
        op, rd, ra, rb, immediate = (instruction >> 24, (instruction >> 22) & 3,
                                    (instruction >> 20) & 3, (instruction >> 18) & 3,
                                    instruction & 0xffff)
        fault = op > 8 or (op == 7 and immediate == 0) or (op == 8 and loop == 0)
        if fault:
            return Execution(memory, retirements, transfers, snapshot(registers, pc, loop), attempt, True)
        if op == 7:
            loop = immediate
        elif op == 8:
            loop -= 1
            if loop:
                pc = immediate % 256
        elif op:
            for lane, row in enumerate(registers):
                if op == 1:
                    row[rd] = immediate
                elif op == 2:
                    row[rd] = lane
                elif op == 3:
                    row[rd] = (row[ra] + row[rb]) % 65536
                elif op == 4:
                    row[rd] = (row[ra] + immediate) % 65536
                elif op in (5, 6):
                    location = (row[ra] + immediate) % 256
                    data = memory[location] if op == 5 else row[rd]
                    transfers.append(((op == 6) << 24) | (location << 16) | data)
                    if op == 5:
                        row[rd] = data
                    else:
                        memory[location] = data
        state = snapshot(registers, pc, loop)
        retirements.append(state | (instruction << (lanes * 64 + 24)) |
                           (address << (lanes * 64 + 56)))
        if op == 0:
            return Execution(memory, retirements, transfers, state, attempt, False)
    raise ValueError("reference program exceeded instruction limit")


def image(words):
    if len(words) > 256:
        raise ValueError("program exceeds 256 words")
    return words + [0] * (256 - len(words))
