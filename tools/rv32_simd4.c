#include "rv32_simd4.h"

enum { HLT, LDI, LANE, ADD, ADDI, LOAD, STORE, SETLOOP, LOOP, MUL, MAC, MACU, CLRA, RDA };

void simd_reset(simd_device *s)
{
    /* Storage and execution state are separate: adding execution fields cannot
     * accidentally make reset retain them or erase the program/data memories. */
    s->execution = (simd_execution){ .command_tick = true };
}

bool simd_access(simd_device *s, uint32_t address, int width, bool write, uint32_t *value)
{
    if (width != 4 || (address & 3u)) return false;
    if (address >= SIMD_PROGRAM && address - SIMD_PROGRAM < 1024) {
        if (s->execution.state != SIMD_IDLE) return false;
        unsigned index = (address - SIMD_PROGRAM) / 4;
        if (write) s->program[index] = *value;
        else *value = s->program[index];
        return true;
    }
    if (address >= SIMD_DATA && address - SIMD_DATA < 1024) {
        if (s->execution.state != SIMD_IDLE) return false;
        unsigned index = (address - SIMD_DATA) / 4;
        if (write) s->data[index] = (uint16_t)*value;
        else *value = s->data[index];
        return true;
    }
    if (address < SIMD_BASE || address - SIMD_BASE >= 32) return false;
    switch (address - SIMD_BASE) {
    case SIMD_COMMAND:
        if (!write || (*value != SIMD_START && *value != SIMD_RESET) || (*value == SIMD_START && s->execution.state != SIMD_IDLE)) return false;
        if (*value == SIMD_RESET) simd_reset(s);
        else {
            uint8_t entry = s->execution.entry;
            simd_reset(s);
            s->execution.entry = s->execution.pc = entry;
            s->execution.state = SIMD_FETCH;
        }
        return true;
    case SIMD_ENTRY:
        if (write) {
            if (s->execution.state != SIMD_IDLE || *value > 255) return false;
            s->execution.entry = (uint8_t)*value;
        } else *value = s->execution.entry;
        return true;
    default:
        if (write) return false;
        switch (address - SIMD_BASE) {
        case SIMD_STATUS: *value = (s->execution.state != SIMD_IDLE ? SIMD_BUSY : 0) | (s->execution.done ? SIMD_DONE : 0) | (s->execution.fault ? SIMD_FAULT : 0); break;
        case SIMD_CYCLES: *value = s->execution.cycles; break;
        case SIMD_STALLS: *value = s->execution.stalls; break;
        case SIMD_TRANSFERS: *value = s->execution.transfers; break;
        case SIMD_INSTRUCTIONS: *value = s->execution.instructions; break;
        default: return false;
        }
        return true;
    }
}

static int32_t signed16(uint16_t x)
{
    return (int32_t)x - ((x & 0x8000u) ? 65536 : 0);
}

void simd_tick(simd_device *s, bool hold)
{
    if (s->execution.command_tick) { s->execution.command_tick = false; return; }
    if (s->execution.state == SIMD_IDLE) return;
    s->execution.cycles++;
    if (s->execution.state == SIMD_FETCH) {
        s->execution.instruction = s->program[s->execution.pc++];
        s->execution.state = SIMD_EXECUTE;
        return;
    }
    uint32_t word = s->execution.instruction;
    unsigned op = word >> 24, rd = (word >> 22) & 3, ra = (word >> 20) & 3, rb = (word >> 18) & 3;
    uint16_t imm = (uint16_t)word;
    if (s->execution.state == SIMD_MEMORY) {
        if (hold) { s->execution.stalls++; return; }
        uint16_t *r = s->execution.r[s->execution.lane];
        uint8_t address = (uint8_t)(r[ra] + imm);
        if (op == STORE) s->data[address] = r[rd];
        else r[rd] = s->data[address];
        s->execution.transfers++;
        if (++s->execution.lane < 4) return;
        s->execution.lane = 0;
    } else {
        if (op == LOAD || op == STORE) { s->execution.lane = 0; s->execution.state = SIMD_MEMORY; return; }
        if (op > RDA || (op == SETLOOP && imm == 0) || (op == LOOP && s->execution.loop == 0)) {
            s->execution.state = SIMD_IDLE;
            s->execution.done = s->execution.fault = true;
            return;
        }
        if (op == SETLOOP) s->execution.loop = imm;
        if (op == LOOP && --s->execution.loop) s->execution.pc = (uint8_t)imm;
        for (unsigned lane = 0; lane < 4; lane++) {
            uint16_t *r = s->execution.r[lane];
            switch (op) {
            case LDI: r[rd] = imm; break;
            case LANE: r[rd] = (uint16_t)lane; break;
            case ADD: r[rd] = (uint16_t)(r[ra] + r[rb]); break;
            case ADDI: r[rd] = (uint16_t)(r[ra] + imm); break;
            case MUL: r[rd] = (uint16_t)((uint32_t)r[ra] * r[rb]); break;
            case MAC: s->execution.acc[lane] += (uint32_t)(signed16(r[ra]) * signed16(r[rb])); break;
            case MACU: s->execution.acc[lane] += (uint32_t)r[ra] * r[rb]; break;
            case CLRA: s->execution.acc[lane] = 0; break;
            case RDA: {
                unsigned shift = imm & 31;
                uint32_t value = s->execution.acc[lane] >> shift;
                if (shift && (s->execution.acc[lane] & 0x80000000u)) value |= UINT32_MAX << (32 - shift);
                r[rd] = (uint16_t)value;
                break;
            }
            default: break;
            }
        }
    }
    s->execution.instructions++;
    s->execution.state = op == HLT ? SIMD_IDLE : SIMD_FETCH;
    if (op == HLT) s->execution.done = true;
}
