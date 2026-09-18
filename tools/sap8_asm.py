#!/usr/bin/env python3
"""Assemble SAP8 source into separate 256-word program and data hex images."""

import argparse
from pathlib import Path
import re


OPCODES = {"LDI": 0, "LDA": 1, "STA": 2, "ADD": 3, "SUB": 4,
           "JMP": 5, "JZ": 6, "OUT": 7, "HLT": 8}
LABEL = re.compile(r"[A-Za-z_][A-Za-z_0-9]*\Z")


class AssemblyError(ValueError):
    """A source error with a line number."""


def fail(line, message):
    raise AssemblyError(f"line {line}: {message}")


def byte(token, line):
    try:
        base = 16 if token.lower().startswith("0x") else 2 if token.lower().startswith("0b") else 10
        value = int(token, base)
    except ValueError:
        fail(line, f"unknown label or invalid integer: {token!r}")
    if not 0 <= value <= 255:
        fail(line, f"operand must be in 0..255: {token}")
    return value


def assemble(source):
    """Return (program words, data bytes); labels are program word addresses."""
    labels = {}
    instructions = []
    data = [0] * 256
    initialized = set()
    for line_number, raw in enumerate(source.splitlines(), 1):
        text = raw.partition(";")[0].strip()
        if not text:
            continue
        if ":" in text:
            label, _, text = text.partition(":")
            label, text = label.strip(), text.strip()
            if not LABEL.fullmatch(label):
                fail(line_number, f"invalid label: {label!r}")
            if label in labels:
                fail(line_number, f"duplicate label: {label}")
            if len(instructions) >= 256:
                fail(line_number, "label address exceeds 255")
            if text.lower().startswith(".data"):
                fail(line_number, "labels name program words; .data uses numeric addresses")
            labels[label] = len(instructions)
        if not text:
            continue
        fields = text.split()
        mnemonic = fields[0].upper()
        if mnemonic == ".DATA":
            if len(fields) != 3:
                fail(line_number, "expected .data address value")
            address, value = (byte(token, line_number) for token in fields[1:])
            if address in initialized:
                fail(line_number, f"duplicate data address: {address}")
            data[address] = value
            initialized.add(address)
            continue
        if mnemonic not in OPCODES:
            fail(line_number, f"unknown instruction: {fields[0]}")
        count = 1 if mnemonic in ("OUT", "HLT") else 2
        if len(fields) != count:
            fail(line_number, f"{mnemonic} requires {count - 1} operand(s)")
        if len(instructions) >= 256:
            fail(line_number, "program exceeds 256 words")
        instructions.append((line_number, mnemonic, fields[1] if count == 2 else None))

    program = [0x0800] * 256  # Unused program space halts deterministically.
    for address, (line, mnemonic, operand) in enumerate(instructions):
        value = 0 if operand is None else labels[operand] if operand in labels else byte(operand, line)
        program[address] = (OPCODES[mnemonic] << 8) | value
    return program, data


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("--program", type=Path, required=True)
    parser.add_argument("--data", type=Path, required=True)
    args = parser.parse_args()
    try:
        paths = [path.resolve() for path in (args.source, args.program, args.data)]
        if len(set(paths)) != 3:
            raise AssemblyError("source, program output, and data output must be distinct paths")
        program, data = assemble(args.source.read_text())
        for path, values, width in ((args.program, program, 4), (args.data, data, 2)):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("".join(f"{value:0{width}x}\n" for value in values))
    except (AssemblyError, OSError) as error:
        parser.exit(1, f"{args.source}: {error}\n")


if __name__ == "__main__":
    main()
