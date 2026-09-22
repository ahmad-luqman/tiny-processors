#include "rv32_simd4.h"
#include <string.h>

enum { IDLE, FETCH, EXECUTE, MEMORY };
enum { HLT, LDI, LANE, ADD, ADDI, LOAD, STORE, SETLOOP, LOOP, MUL, MAC, MACU, CLRA, RDA };

void simd_reset(simd_device *s)
{
    /* Memories survive either kind of reset, just like RTL storage. */
    memset(s->r, 0, sizeof s->r);
    memset(s->acc, 0, sizeof s->acc);
    s->loop = 0;
    s->instruction = s->cycles = s->stalls = s->transfers = s->instructions = 0;
    s->entry = s->pc = s->state = s->lane = 0;
    s->done = s->fault = false;
    s->command_tick = true;
}

bool simd_access(simd_device *s, uint32_t address, int width, bool write, uint32_t *value)
{
    if (width != 4 || (address & 3u)) return false;
    if (address >= SIMD_PROGRAM && address - SIMD_PROGRAM < 1024) {
        if (s->state != IDLE) return false;
        unsigned index = (address - SIMD_PROGRAM) / 4;
        if (write) s->program[index] = *value;
        else *value = s->program[index];
        return true;
    }
    if (address >= SIMD_DATA && address - SIMD_DATA < 1024) {
        if (s->state != IDLE) return false;
        unsigned index = (address - SIMD_DATA) / 4;
        if (write) s->data[index] = (uint16_t)*value;
        else *value = s->data[index];
        return true;
    }
    if (address < SIMD_BASE || address - SIMD_BASE >= 32) return false;
    switch (address - SIMD_BASE) {
    case 0:
        if (!write || (*value != 1 && *value != 2) || (*value == 1 && s->state != IDLE)) return false;
        if (*value == 2) simd_reset(s);
        else {
            uint8_t entry = s->entry;
            simd_reset(s);
            s->entry = s->pc = entry;
            s->state = FETCH;
        }
        return true;
    case 8:
        if (write) {
            if (s->state != IDLE || *value > 255) return false;
            s->entry = (uint8_t)*value;
        } else *value = s->entry;
        return true;
    default:
        if (write) return false;
        switch (address - SIMD_BASE) {
        case 4: *value = (s->state != IDLE) | ((uint32_t)s->done << 1) | ((uint32_t)s->fault << 2); break;
        case 12: *value = s->cycles; break;
        case 16: *value = s->stalls; break;
        case 20: *value = s->transfers; break;
        case 24: *value = s->instructions; break;
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
    if (s->command_tick) { s->command_tick = false; return; }
    if (s->state == IDLE) return;
    s->cycles++;
    if (s->state == FETCH) {
        s->instruction = s->program[s->pc++];
        s->state = EXECUTE;
        return;
    }
    uint32_t word = s->instruction;
    unsigned op = word >> 24, rd = (word >> 22) & 3, ra = (word >> 20) & 3, rb = (word >> 18) & 3;
    uint16_t imm = (uint16_t)word;
    if (s->state == MEMORY) {
        if (hold) { s->stalls++; return; }
        uint16_t *r = s->r[s->lane];
        uint8_t address = (uint8_t)(r[ra] + imm);
        if (op == STORE) s->data[address] = r[rd];
        else r[rd] = s->data[address];
        s->transfers++;
        if (++s->lane < 4) return;
        s->lane = 0;
    } else {
        if (op == LOAD || op == STORE) { s->lane = 0; s->state = MEMORY; return; }
        if (op > RDA || (op == SETLOOP && imm == 0) || (op == LOOP && s->loop == 0)) {
            s->state = IDLE;
            s->done = s->fault = true;
            return;
        }
        if (op == SETLOOP) s->loop = imm;
        if (op == LOOP && --s->loop) s->pc = (uint8_t)imm;
        for (unsigned lane = 0; lane < 4; lane++) {
            uint16_t *r = s->r[lane];
            switch (op) {
            case LDI: r[rd] = imm; break;
            case LANE: r[rd] = (uint16_t)lane; break;
            case ADD: r[rd] = (uint16_t)(r[ra] + r[rb]); break;
            case ADDI: r[rd] = (uint16_t)(r[ra] + imm); break;
            case MUL: r[rd] = (uint16_t)((uint32_t)r[ra] * r[rb]); break;
            case MAC: s->acc[lane] += (uint32_t)(signed16(r[ra]) * signed16(r[rb])); break;
            case MACU: s->acc[lane] += (uint32_t)r[ra] * r[rb]; break;
            case CLRA: s->acc[lane] = 0; break;
            case RDA: {
                unsigned shift = imm & 31;
                uint32_t value = s->acc[lane] >> shift;
                if (shift && (s->acc[lane] & 0x80000000u)) value |= UINT32_MAX << (32 - shift);
                r[rd] = (uint16_t)value;
                break;
            }
            default: break;
            }
        }
    }
    s->instructions++;
    s->state = op == HLT ? IDLE : FETCH;
    if (op == HLT) s->done = true;
}
