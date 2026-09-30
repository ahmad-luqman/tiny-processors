/* rv32_gdb: the GDB remote serial protocol stub; see rv32_gdb.h for the API and
 * docs/rv32-gdb.md for the record (packet set, register numbering, stop semantics).
 *
 * The stub sits outside the machine. It runs the guest only through emu_run_until, so a step
 * under the debugger is the same step, trace line, timer tick, and accelerator tick as a step
 * without it; it reads and writes memory only through emu_debug_read/write, which never touch a
 * device; and it keeps breakpoints in its own table instead of patching ebreak into guest RAM,
 * which would change what the guest reads, what the trace records, and which traps it takes.
 */
#define _POSIX_C_SOURCE 200809L
#ifdef __APPLE__
#define _DARWIN_C_SOURCE 1 /* keep the BSD socket names visible under the strict POSIX define */
#endif
#include "rv32_gdb.h"

#include <assert.h>
#include <errno.h>
#include <inttypes.h>
#include <poll.h>
#include <signal.h>
#include <stdarg.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <arpa/inet.h>
#include <netinet/in.h>
#include <netinet/tcp.h>
#include <sys/socket.h>

/* The largest packet payload either side sends, advertised as PacketSize (hex 4000). The g
 * reply is 520 characters, so this only bounds memory transfers and the target description. */
#define PACKET_SIZE 0x4000u
/* The longest m or qXfer reply payload: hex doubles each byte, and escaping at worst doubles a
 * binary one, with room left for the m/l prefix. A longer request gets this much; gdb asks
 * again for the rest. */
#define MAX_READ ((PACKET_SIZE - 16u) / 2u)
/* A running guest checks the socket for Ctrl-C once per this many instructions: one poll()
 * system call per batch has no measurable cost, and an interrupt still lands within about a
 * millisecond (docs/rv32-gdb.md, "The cost of Ctrl-C polling"). */
#define POLL_INTERVAL 65536u
#define MAX_BREAKPOINTS 64u
/* gdb's RISC-V register numbers: x0-x31 are 0-31, pc 32, f0-f31 33-64, and CSR n is 65 + n.
 * The g packet carries the contiguous block 0-64; everything above is read with p. */
#define REG_PC 32u
#define REG_F0 33u
#define REG_CSR0 65u
#define G_REGS 65u

typedef enum { STOP_STEP, STOP_BREAK, STOP_INTERRUPT, STOP_HALTED, STOP_GONE } stop_reason;

/* Z0 and Z1: one table, each entry reported under its own stop reason. */
typedef enum { BP_SW, BP_HW } breakpoint_kind;

typedef struct {
    uint32_t addr;
    breakpoint_kind kind;
} breakpoint;

typedef struct {
    int fd;
    machine *m;
    bool ack;       /* acknowledgement mode: each packet is answered with + or - (until QStartNoAckMode) */
    bool gone;      /* the connection closed or failed */
    bool swbreak;   /* the client asked for swbreak/hwbreak stop reasons in qSupported */
    bool at_break;  /* the last stop was a breakpoint at break_pc: the next continue steps past it */
    uint32_t break_pc;
    uint8_t in[4096];
    size_t in_len, in_pos;
    breakpoint bp[MAX_BREAKPOINTS];
    size_t bps;
    char packet[PACKET_SIZE + 1]; /* the current request's payload, NUL terminated */
    size_t packet_len;
    char out[2 * PACKET_SIZE + 8]; /* the reply payload; binary escapes can double it */
    size_t out_len;
    gdb_end end;    /* how the session ended, for gdb_report_exit */
} gdb_state;

static const char HEX[] = "0123456789abcdef";

/* ---- the target description ------------------------------------------------------------ */

static const char *const XREG_NAMES[32] = {
    "zero", "ra", "sp", "gp", "tp", "t0", "t1", "t2", "fp", "s1", "a0", "a1", "a2", "a3", "a4", "a5",
    "a6", "a7", "s2", "s3", "s4", "s5", "s6", "s7", "s8", "s9", "s10", "s11", "t3", "t4", "t5", "t6"};
static const char *const FREG_NAMES[32] = {
    "ft0", "ft1", "ft2", "ft3", "ft4", "ft5", "ft6", "ft7", "fs0", "fs1", "fa0", "fa1", "fa2", "fa3", "fa4", "fa5",
    "fa6", "fa7", "fs2", "fs3", "fs4", "fs5", "fs6", "fs7", "fs8", "fs9", "fs10", "fs11", "ft8", "ft9", "ft10", "ft11"};
/* The machine CSRs of the trap contract and of interrupts (O1); the floating ones live in the fpu
 * feature, as in gdb's own features/riscv/32bit-fpu.xml. mip is read-only in effect: a write is
 * accepted and changes nothing, as a csrw does. */
static const struct { const char *name; uint32_t number; } CSR_REGS[] = {
    {"mstatus", 0x300}, {"mie", 0x304}, {"mtvec", 0x305}, {"mscratch", 0x340}, {"mepc", 0x341}, {"mcause", 0x342},
    {"mtval", 0x343}, {"mip", 0x344},
    /* Zicntr: read-only, so a debugger write is refused with an error, as a csrw would trap */
    {"cycle", 0xc00}, {"time", 0xc01}, {"instret", 0xc02}, {"cycleh", 0xc80}, {"timeh", 0xc81}, {"instreth", 0xc82}};

static char tdesc[8192];
static size_t tdesc_len;

static void tdesc_add(const char *format, ...)
{
    va_list args;
    va_start(args, format);
    int n = vsnprintf(tdesc + tdesc_len, sizeof tdesc - tdesc_len, format, args);
    va_end(args);
    if (n < 0 || (size_t)n >= sizeof tdesc - tdesc_len) {
        fprintf(stderr, "%s: gdb target description does not fit\n", emu_prog);
        exit(EXIT_EMULATOR_ERROR); /* a build-time mistake, never a runtime condition */
    }
    tdesc_len += (size_t)n;
}

/* The description gdb fetches with qXfer:features:read. Every register carries its regnum so
 * the CSRs can sit at 65 + number without filler registers for the gaps. */
static void build_tdesc(void)
{
    if (tdesc_len) {
        return;
    }
    tdesc_add("<?xml version=\"1.0\"?>\n<!DOCTYPE target SYSTEM \"gdb-target.dtd\">\n<target version=\"1.0\">\n"
              "<architecture>riscv:rv32</architecture>\n<feature name=\"org.gnu.gdb.riscv.cpu\">\n");
    for (unsigned i = 0; i < 32; i++) {
        const char *type = i == 1 ? "code_ptr" : (i == 2 || i == 3 || i == 4 || i == 8) ? "data_ptr" : "int";
        tdesc_add("<reg name=\"%s\" bitsize=\"32\" type=\"%s\" regnum=\"%u\"/>\n", XREG_NAMES[i], type, i);
    }
    tdesc_add("<reg name=\"pc\" bitsize=\"32\" type=\"code_ptr\" regnum=\"%u\"/>\n</feature>\n"
              "<feature name=\"org.gnu.gdb.riscv.fpu\">\n", REG_PC);
    for (unsigned i = 0; i < 32; i++) {
        tdesc_add("<reg name=\"%s\" bitsize=\"32\" type=\"ieee_single\" regnum=\"%u\"/>\n", FREG_NAMES[i], REG_F0 + i);
    }
    tdesc_add("<reg name=\"fflags\" bitsize=\"32\" type=\"int\" regnum=\"%u\"/>\n"
              "<reg name=\"frm\" bitsize=\"32\" type=\"int\" regnum=\"%u\"/>\n"
              "<reg name=\"fcsr\" bitsize=\"32\" type=\"int\" regnum=\"%u\"/>\n</feature>\n"
              "<feature name=\"org.gnu.gdb.riscv.csr\">\n", REG_CSR0 + 1, REG_CSR0 + 2, REG_CSR0 + 3);
    for (size_t i = 0; i < sizeof CSR_REGS / sizeof CSR_REGS[0]; i++) {
        tdesc_add("<reg name=\"%s\" bitsize=\"32\" type=\"int\" regnum=\"%" PRIu32 "\"/>\n", CSR_REGS[i].name,
                  REG_CSR0 + CSR_REGS[i].number);
    }
    tdesc_add("</feature>\n</target>\n");
}

/* ---- registers --------------------------------------------------------------------------- */

static bool reg_read(const machine *m, uint32_t regnum, uint32_t *value)
{
    if (regnum < 32) {
        *value = m->x[regnum];
    } else if (regnum == REG_PC) {
        *value = m->pc;
    } else if (regnum < REG_F0 + 32) {
        *value = m->f[regnum - REG_F0];
    } else if (regnum - REG_CSR0 < 4096) {
        return emu_csr_read(m, regnum - REG_CSR0, value);
    } else {
        return false;
    }
    return true;
}

/* x0 stays zero (the write is accepted and discarded, as an instruction's is); CSRs go through
 * the same WARL masks as CSRRW. */
static bool reg_write(machine *m, uint32_t regnum, uint32_t value)
{
    if (regnum < 32) {
        if (regnum) {
            m->x[regnum] = value;
        }
    } else if (regnum == REG_PC) {
        m->pc = value;
    } else if (regnum < REG_F0 + 32) {
        m->f[regnum - REG_F0] = value;
    } else if (regnum - REG_CSR0 < 4096) {
        return emu_csr_write(m, regnum - REG_CSR0, value);
    } else {
        return false;
    }
    return true;
}

/* ---- the connection ---------------------------------------------------------------------- */

/* The first failure ends the connection and says why on stderr (error is its errno, 0 when the
 * client closed the connection); later calls only see gone. */
static void connection_lost(gdb_state *g, int error)
{
    if (g->gone) {
        return;
    }
    g->gone = true;
    if (error) {
        fprintf(stderr, "%s: gdb: connection lost: %s\n", emu_prog, strerror(error));
    } else {
        fprintf(stderr, "%s: gdb: connection closed by client\n", emu_prog);
    }
}

static bool send_all(gdb_state *g, const void *data, size_t len)
{
    const char *p = data;
    while (len > 0 && !g->gone) {
        ssize_t n = send(g->fd, p, len, 0);
        if (n < 0 && errno == EINTR) {
            continue;
        }
        if (n <= 0) {
            connection_lost(g, n < 0 ? errno : EPIPE);
            break;
        }
        p += n;
        len -= (size_t)n;
    }
    return !g->gone;
}

/* One byte, blocking; -1 once the connection is gone. */
static int next_byte(gdb_state *g)
{
    while (g->in_pos == g->in_len) {
        if (g->gone) {
            return -1;
        }
        ssize_t n = recv(g->fd, g->in, sizeof g->in, 0);
        if (n < 0 && errno == EINTR) {
            continue;
        }
        if (n <= 0) {
            connection_lost(g, n < 0 ? errno : 0);
            return -1;
        }
        g->in_len = (size_t)n;
        g->in_pos = 0;
    }
    return g->in[g->in_pos++];
}

/* True when a byte can be read without blocking (buffered, readable, or the peer hung up). */
static bool byte_ready(gdb_state *g)
{
    if (g->in_pos < g->in_len) {
        return true;
    }
    struct pollfd p = {.fd = g->fd, .events = POLLIN};
    int n;
    do {
        n = poll(&p, 1, 0);
    } while (n < 0 && errno == EINTR);
    return n > 0;
}

static int hex_digit(int c)
{
    if (c >= '0' && c <= '9') return c - '0';
    if (c >= 'a' && c <= 'f') return c - 'a' + 10;
    if (c >= 'A' && c <= 'F') return c - 'A' + 10;
    return -1;
}

/* Two hex digits at p as a byte, or -1 (p[1] is not read when p[0] is not a digit). */
static int hex_byte(const char *p)
{
    int hi = hex_digit((unsigned char)p[0]);
    int lo = hi < 0 ? -1 : hex_digit((unsigned char)p[1]);
    return hi < 0 || lo < 0 ? -1 : hi * 16 + lo;
}

/* Send the reply in g->out, framed and checksummed; in ack mode resend on `-`. A client that
 * answers anything else (a stray Ctrl-C) is ignored until it acknowledges. */
static void send_reply(gdb_state *g)
{
    char frame[sizeof g->out + 4];
    uint8_t sum = 0;
    frame[0] = '$';
    memcpy(frame + 1, g->out, g->out_len);
    for (size_t i = 0; i < g->out_len; i++) {
        sum = (uint8_t)(sum + (uint8_t)g->out[i]);
    }
    size_t len = g->out_len + 1;
    frame[len++] = '#';
    frame[len++] = HEX[sum >> 4];
    frame[len++] = HEX[sum & 15];
    for (int attempt = 0; attempt < 16; attempt++) {
        if (!send_all(g, frame, len) || !g->ack) {
            return;
        }
        for (;;) {
            int c = next_byte(g);
            if (c < 0 || c == '+') {
                return;
            }
            if (c == '-') {
                break;
            }
        }
    }
    fprintf(stderr, "%s: gdb: client rejected a reply 16 times; closing\n", emu_prog);
    g->gone = true; /* already said why */
}

static void reply(gdb_state *g, const char *text);

/* Wait for the next request; false once the connection is gone. Bytes outside a packet (acks,
 * a late Ctrl-C) are ignored while stopped.
 * In acknowledgement mode a bad checksum, a non-hex checksum digit, or a payload longer than
 * PACKET_SIZE is answered with `-` and dropped, and gdb retransmits.
 * In no-ack mode nothing would be retransmitted, so a dropped packet would leave gdb waiting
 * for a reply forever: the checksum is not verified (the protocol lets the receiver ignore it
 * there; the transport is a reliable TCP stream), and an oversized packet is answered with
 * E01 and logged. */
static bool read_packet(gdb_state *g)
{
    for (;;) {
        int c = next_byte(g);
        if (c < 0) {
            return false;
        }
        if (c != '$') {
            continue;
        }
        size_t len = 0;
        bool overflow = false;
        uint8_t sum = 0;
        while ((c = next_byte(g)) >= 0 && c != '#') {
            sum = (uint8_t)(sum + c);
            if (len < PACKET_SIZE) {
                g->packet[len++] = (char)c;
            } else {
                overflow = true;
            }
        }
        if (c < 0) {
            return false;
        }
        char digits[2];
        digits[0] = (char)next_byte(g); /* -1 once gone: not a hex digit, and gone is checked */
        digits[1] = (char)next_byte(g);
        if (g->gone) {
            return false;
        }
        if (overflow) {
            fprintf(stderr, "%s: gdb: dropped a packet longer than PacketSize (%u bytes)\n", emu_prog,
                    (unsigned)PACKET_SIZE);
        }
        if (g->ack && (overflow || hex_byte(digits) != sum)) {
            send_all(g, "-", 1);
            continue;
        }
        if (overflow) { /* no-ack mode */
            reply(g, "E01");
            send_reply(g);
            continue;
        }
        if (g->ack && !send_all(g, "+", 1)) {
            return false;
        }
        g->packet[len] = '\0';
        g->packet_len = len;
        return true;
    }
}

/* ---- reply builders ---------------------------------------------------------------------- */

static void reply(gdb_state *g, const char *text)
{
    size_t n = strlen(text);
    memcpy(g->out, text, n);
    g->out_len = n;
}

static void reply_format(gdb_state *g, const char *format, ...)
{
    va_list args;
    va_start(args, format);
    int n = vsnprintf(g->out, sizeof g->out, format, args);
    va_end(args);
    g->out_len = n < 0 ? 0 : (size_t)n; /* every format here is a short constant shape */
}

static void reply_add_hex_byte(gdb_state *g, uint8_t byte)
{
    g->out[g->out_len++] = HEX[byte >> 4];
    g->out[g->out_len++] = HEX[byte & 15];
}

/* A register value in target (little-endian) byte order. */
static void reply_add_reg(gdb_state *g, uint32_t value)
{
    for (int i = 0; i < 4; i++) {
        reply_add_hex_byte(g, (uint8_t)(value >> (8 * i)));
    }
}

/* Binary data in a reply: `$`, `#`, `}` and `*` (which gdb reads as run-length encoding) are
 * escaped as `}` followed by the byte xor 0x20. */
static void reply_add_binary(gdb_state *g, const char *data, size_t n)
{
    for (size_t i = 0; i < n; i++) {
        char c = data[i];
        if (c == '$' || c == '#' || c == '}' || c == '*') {
            g->out[g->out_len++] = '}';
            c ^= 0x20;
        }
        g->out[g->out_len++] = c;
    }
}

/* ---- request parsing --------------------------------------------------------------------- */

/* A hex number of 1..8 digits at *p that fits 32 bits; advances *p. */
static bool parse_hex(const char **p, uint32_t *value)
{
    uint32_t v = 0;
    int digits = 0, d;
    while ((d = hex_digit((unsigned char)**p)) >= 0) {
        if (++digits > 8) {
            return false;
        }
        v = (v << 4) | (uint32_t)d;
        (*p)++;
    }
    *value = v;
    return digits > 0;
}

/* `addr,length` followed by `end` (':' for M/X, '\0' for m). */
static bool parse_range(const char **p, uint32_t *addr, uint32_t *length, char end)
{
    if (!parse_hex(p, addr) || **p != ',') {
        return false;
    }
    (*p)++;
    if (!parse_hex(p, length) || **p != end) {
        return false;
    }
    return true;
}

/* A little-endian register value of exactly eight hex digits. */
static bool parse_reg(const char *p, uint32_t *value)
{
    uint32_t v = 0;
    for (int i = 0; i < 4; i++) {
        int byte = hex_byte(p + 2 * i);
        if (byte < 0) {
            return false;
        }
        v |= (uint32_t)byte << (8 * i);
    }
    *value = v;
    return true;
}

/* ---- execution --------------------------------------------------------------------------- */

static const breakpoint *find_breakpoint(const gdb_state *g, uint32_t addr)
{
    for (size_t i = 0; i < g->bps; i++) {
        if (g->bp[i].addr == addr) {
            return &g->bp[i];
        }
    }
    return NULL;
}

/* Drain the bytes that arrived while the guest ran: true on Ctrl-C (0x03). Anything else is a
 * protocol slip in all-stop mode and is dropped; a hang-up marks the connection gone. */
static bool interrupted(gdb_state *g)
{
    bool hit = false;
    while (!g->gone && byte_ready(g)) {
        int c = next_byte(g);
        if (c == 0x03) {
            hit = true;
        }
    }
    return hit;
}

/* One instruction step of the machine (a trap counts: pc is then at mtvec), or a continue: run
 * until a breakpoint address is about to execute, Ctrl-C, or a halt. Every pc is checked before
 * it executes, the first one included: a breakpoint at the reset pc, or at a pc the client wrote,
 * stops the continue at once. The one exception is `from_break`, a continue from the breakpoint
 * that caused the last stop, which executes that instruction first so it makes progress. Without
 * breakpoints the guest runs in POLL_INTERVAL batches through emu_run_until at full speed;
 * with them it runs one step at a time so each pc can be checked before it executes. */
static stop_reason resume(gdb_state *g, bool stepping, bool from_break, const breakpoint **hit)
{
    machine *m = g->m;
    if (stepping) {
        return emu_run_until(m, 1) == EMU_STOP_HALTED ? STOP_HALTED : STOP_STEP;
    }
    uint64_t next_poll = m->steps + POLL_INTERVAL;
    bool check = !from_break;
    for (;;) {
        if (check && (*hit = find_breakpoint(g, m->pc)) != NULL) {
            return STOP_BREAK;
        }
        check = true;
        if (emu_run_until(m, g->bps ? 1 : POLL_INTERVAL) == EMU_STOP_HALTED) {
            return STOP_HALTED;
        }
        if (m->steps >= next_poll) { /* a present returns early; poll on instructions, not returns */
            next_poll = m->steps + POLL_INTERVAL;
            if (interrupted(g)) {
                return STOP_INTERRUPT;
            }
            if (g->gone) {
                return STOP_GONE;
            }
        }
    }
}

/* ---- the command loop -------------------------------------------------------------------- */

static void handle_query(gdb_state *g)
{
    static const char XFER[] = "qXfer:features:read:", ANNEX[] = "target.xml:";
    const char *p = g->packet;
    if (!strncmp(p, "qSupported", 10)) {
        g->swbreak = strstr(p, "swbreak+") != NULL;
        reply_format(g, "PacketSize=%x;qXfer:features:read+;swbreak+;hwbreak+;QStartNoAckMode+;vContSupported+",
                     PACKET_SIZE);
    } else if (!strncmp(p, XFER, sizeof XFER - 1)) {
        const char *q = p + sizeof XFER - 1;
        uint32_t offset, length;
        if (strncmp(q, ANNEX, sizeof ANNEX - 1)) {
            reply(g, "E00"); /* the only annex there is */
            return;
        }
        q += sizeof ANNEX - 1;
        if (!parse_range(&q, &offset, &length, '\0')) {
            reply(g, "E01");
            return;
        }
        build_tdesc();
        if (offset > tdesc_len) {
            reply(g, "E01");
            return;
        }
        size_t n = tdesc_len - offset;
        if (length > MAX_READ) {
            length = MAX_READ;
        }
        if (n > length) {
            n = length;
        }
        g->out[0] = offset + n < tdesc_len ? 'm' : 'l';
        g->out_len = 1;
        reply_add_binary(g, tdesc + offset, n);
    } else if (!strcmp(p, "qAttached")) {
        reply(g, "1"); /* attached to an existing process: detach leaves it running, kill ends it */
    } else if (!strcmp(p, "qC")) {
        reply(g, "QC1");
    } else if (!strcmp(p, "qfThreadInfo")) {
        reply(g, "m1");
    } else if (!strcmp(p, "qsThreadInfo")) {
        reply(g, "l");
    } else if (!strcmp(p, "qOffsets")) {
        reply(g, "Text=0;Data=0;Bss=0"); /* the ELF is linked where it runs */
    } else if (!strncmp(p, "qSymbol", 7)) {
        reply(g, "OK");
    } else {
        reply(g, "");
    }
}

/* m, M, X: memory through the side-effect-free debugger path; a refused address is E01. */
static void handle_memory(gdb_state *g)
{
    const char *p = g->packet + 1;
    char kind = g->packet[0];
    uint32_t addr, length;
    if (!parse_range(&p, &addr, &length, kind == 'm' ? '\0' : ':')) {
        reply(g, "E01");
        return;
    }
    if (kind == 'm') {
        uint8_t data[MAX_READ];
        if (length > MAX_READ) {
            length = MAX_READ; /* a shorter reply is allowed; gdb asks for the rest */
        }
        if (!emu_debug_read(g->m, addr, data, length)) {
            reply(g, "E01");
            return;
        }
        g->out_len = 0;
        for (uint32_t i = 0; i < length; i++) {
            reply_add_hex_byte(g, data[i]);
        }
        return;
    }
    p++; /* past ':' */
    uint8_t data[PACKET_SIZE];
    size_t n = 0;
    const char *end = g->packet + g->packet_len;
    if (length > sizeof data) {
        reply(g, "E01");
        return;
    }
    if (kind == 'M') {
        if ((size_t)(end - p) != 2 * (size_t)length) {
            reply(g, "E01");
            return;
        }
        for (; n < length; n++) {
            int byte = hex_byte(p + 2 * n);
            if (byte < 0) {
                reply(g, "E01");
                return;
            }
            data[n] = (uint8_t)byte;
        }
    } else { /* X: binary, `}` escapes the next byte xor 0x20 */
        while (p < end && n < sizeof data) {
            uint8_t c = (uint8_t)*p++;
            if (c == '}') {
                if (p == end) {
                    reply(g, "E01");
                    return;
                }
                c = (uint8_t)(*p++ ^ 0x20);
            }
            data[n++] = c;
        }
        if (n != length || p != end) {
            reply(g, "E01");
            return;
        }
        if (length == 0) { /* gdb's probe for X support */
            reply(g, "OK");
            return;
        }
    }
    reply(g, emu_debug_write(g->m, addr, data, length) ? "OK" : "E01");
}

static void handle_breakpoint(gdb_state *g)
{
    const char *p = g->packet + 2;
    bool insert = g->packet[0] == 'Z';
    uint32_t addr, size;
    if (g->packet[1] != '0' && g->packet[1] != '1') {
        reply(g, ""); /* watchpoints (2-4) are not supported */
        return;
    }
    breakpoint_kind kind = g->packet[1] == '0' ? BP_SW : BP_HW;
    if (*p++ != ',' || !parse_hex(&p, &addr) || *p++ != ',' || !parse_hex(&p, &size) || (*p != '\0' && *p != ';')) {
        reply(g, "E01");
        return;
    }
    for (size_t i = 0; i < g->bps; i++) {
        if (g->bp[i].addr == addr && g->bp[i].kind == kind) {
            if (!insert) {
                g->bp[i] = g->bp[--g->bps];
            }
            reply(g, "OK"); /* inserting twice is idempotent */
            return;
        }
    }
    if (!insert || g->bps == MAX_BREAKPOINTS) {
        reply(g, "E01"); /* removing one that is not there, or the table is full */
        return;
    }
    g->bp[g->bps++] = (breakpoint){addr, kind};
    reply(g, "OK");
}

/* Parse the resume requests into (stepping, optional new pc): c/s [addr], C/S sig[;addr], and
 * vCont;action[:thread][;...] where only the first action matters (there is one thread). */
static bool parse_resume(gdb_state *g, bool *stepping, bool *has_addr, uint32_t *addr)
{
    const char *p = g->packet;
    *has_addr = false;
    if (!strncmp(p, "vCont;", 6)) {
        char action = p[6];
        if (action != 'c' && action != 's' && action != 'C' && action != 'S') {
            return false;
        }
        *stepping = action == 's' || action == 'S';
        return true;
    }
    char command = *p++;
    *stepping = command == 's' || command == 'S';
    if (command == 'C' || command == 'S') {
        uint32_t signal_number;
        if (!parse_hex(&p, &signal_number)) { /* the signal is ignored: nothing delivers it */
            return false;
        }
        if (*p == ';') {
            p++;
        } else if (*p != '\0') {
            return false;
        }
    }
    if (*p != '\0') {
        if (!parse_hex(&p, addr) || *p != '\0') {
            return false;
        }
        *has_addr = true;
    }
    return true;
}

static void stop_reply(gdb_state *g, stop_reason reason, const breakpoint *hit)
{
    if (reason == STOP_INTERRUPT) {
        reply(g, "T02");
    } else if (reason == STOP_BREAK && g->swbreak) {
        reply(g, hit->kind == BP_SW ? "T05swbreak:;" : "T05hwbreak:;");
    } else {
        reply(g, "T05");
    }
}

/* c, s, C, S, vCont;c/s: resume, then answer with the stop reply. Returns false when the
 * session is over: the guest halted (*end is GDB_HALTED) or the client hung up while it ran. */
static bool handle_resume(gdb_state *g, gdb_end *end)
{
    bool stepping, has_addr;
    uint32_t addr = 0;
    if (!parse_resume(g, &stepping, &has_addr, &addr)) {
        reply(g, "E01");
        return true;
    }
    if (has_addr) {
        g->m->pc = addr;
    }
    /* Only a breakpoint stop at this very pc is stepped over; a pc moved since (by P, G, or
     * a resume address) is checked like any other. */
    bool from_break = g->at_break && g->m->pc == g->break_pc;
    const breakpoint *hit = NULL;
    stop_reason reason = resume(g, stepping, from_break, &hit);
    g->at_break = reason == STOP_BREAK;
    g->break_pc = g->m->pc;
    if (reason == STOP_HALTED) {
        *end = GDB_HALTED; /* main sends W with the exit status once it is known */
        return false;
    }
    if (reason == STOP_GONE) {
        *end = GDB_KILLED;
        return false;
    }
    stop_reply(g, reason, hit);
    return true;
}

/* One session per process, so the state is static: gdb_report_exit needs its socket and ack mode. */
static gdb_state session;
static bool session_used;

gdb_end gdb_serve(int fd, machine *m)
{
    assert(!session_used && "gdb_serve runs once per process");
    session_used = true;
    gdb_state *g = &session;
    memset(g, 0, sizeof *g);
    g->fd = fd;
    g->m = m;
    g->ack = true;
    gdb_end end = GDB_KILLED;
    bool serving = true;
    while (serving && read_packet(g)) {
        const char *p = g->packet;
        g->out_len = 0;
        switch (p[0]) {
        case '?':
            reply(g, "T05"); /* stopped: at reset, or wherever the last stop left it */
            break;
        case 'g':
            for (uint32_t i = 0; i < G_REGS; i++) {
                uint32_t value = 0;
                reg_read(m, i, &value);
                reply_add_reg(g, value);
            }
            break;
        case 'G': {
            size_t count = (g->packet_len - 1) / 8;
            uint32_t values[G_REGS];
            bool ok = (g->packet_len - 1) % 8 == 0 && count <= G_REGS;
            for (size_t i = 0; ok && i < count; i++) {
                ok = parse_reg(p + 1 + 8 * i, &values[i]);
            }
            for (size_t i = 0; ok && i < count; i++) {
                reg_write(m, (uint32_t)i, values[i]);
            }
            reply(g, ok ? "OK" : "E01");
            break;
        }
        case 'p': {
            const char *q = p + 1;
            uint32_t regnum, value;
            if (parse_hex(&q, &regnum) && *q == '\0' && reg_read(m, regnum, &value)) {
                reply_add_reg(g, value);
            } else {
                reply(g, "E01");
            }
            break;
        }
        case 'P': {
            const char *q = p + 1;
            uint32_t regnum, value;
            bool ok = parse_hex(&q, &regnum) && *q++ == '=' && strlen(q) == 8 && parse_reg(q, &value);
            reply(g, ok && reg_write(m, regnum, value) ? "OK" : "E01");
            break;
        }
        case 'm':
        case 'M':
        case 'X':
            handle_memory(g);
            break;
        case 'Z':
        case 'z':
            handle_breakpoint(g);
            break;
        case 'H':
        case 'T':
            reply(g, "OK"); /* one hart, always thread 1, always alive */
            break;
        case 'q':
            handle_query(g);
            break;
        case 'Q':
            if (!strcmp(p, "QStartNoAckMode")) {
                reply(g, "OK");
                send_reply(g); /* this reply is still acknowledged */
                g->ack = false;
                continue;
            }
            reply(g, "");
            break;
        case 'D': /* detach: the run continues on its own and ends like a normal run */
            reply(g, "OK");
            end = GDB_DETACHED;
            serving = false;
            break;
        case 'k': /* kill: no reply is expected */
            end = GDB_KILLED;
            serving = false;
            continue;
        case 'v':
            if (!strcmp(p, "vCont?")) {
                reply(g, "vCont;c;C;s;S");
            } else if (!strncmp(p, "vKill", 5)) {
                reply(g, "OK");
                end = GDB_KILLED;
                serving = false;
            } else if (!strncmp(p, "vCont;", 6)) {
                if (!handle_resume(g, &end)) {
                    serving = false;
                    continue;
                }
            } else {
                reply(g, ""); /* vMustReplyEmpty and every other v packet */
            }
            break;
        case 'c':
        case 's':
        case 'C':
        case 'S':
            if (!handle_resume(g, &end)) {
                serving = false;
                continue;
            }
            break;
        default:
            reply(g, ""); /* unsupported: the empty reply */
            break;
        }
        send_reply(g);
    }
    g->end = end;
    if (end == GDB_HALTED) {
        return end; /* the socket stays open for the W packet */
    }
    if (end == GDB_KILLED) { /* killed (quietly), or the connection was lost (said on stderr) */
        m->halt = HALT_STOPPED;
    }
    close(fd);
    g->fd = -1;
    return end;
}

void gdb_report_exit(int status)
{
    gdb_state *g = &session;
    if (!session_used || g->end != GDB_HALTED || g->fd < 0) {
        return;
    }
    int fd = g->fd;
    reply_format(g, "W%02x", (unsigned)status & 0xffu);
    send_reply(g);
    /* gdb closes its end after an exit; wait for that (briefly) before closing ours, so the
     * reply is not lost to a reset caused by closing with unread bytes. */
    shutdown(fd, SHUT_WR);
    struct pollfd p = {.fd = fd, .events = POLLIN};
    char drain[256];
    while (poll(&p, 1, 1000) > 0 && recv(fd, drain, sizeof drain, 0) > 0) {
    }
    close(fd);
    g->fd = -1;
}

int gdb_accept(uint16_t port)
{
    /* A client that hangs up while a reply is being sent must end the session, not the process. */
    signal(SIGPIPE, SIG_IGN);
    int listener = socket(AF_INET, SOCK_STREAM, 0);
    if (listener < 0) {
        fprintf(stderr, "%s: gdb: cannot create a socket: %s\n", emu_prog, strerror(errno));
        return -1;
    }
    int one = 1;
    setsockopt(listener, SOL_SOCKET, SO_REUSEADDR, &one, sizeof one);
    /* Loopback only: the stub reads and writes the whole machine with no authentication. */
    struct sockaddr_in address = {.sin_family = AF_INET, .sin_port = htons(port)};
    address.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
    socklen_t size = sizeof address;
    if (bind(listener, (struct sockaddr *)&address, sizeof address) != 0 || listen(listener, 1) != 0 ||
        getsockname(listener, (struct sockaddr *)&address, &size) != 0) {
        fprintf(stderr, "%s: gdb: cannot listen on 127.0.0.1:%u: %s\n", emu_prog, (unsigned)port, strerror(errno));
        close(listener);
        return -1;
    }
    fprintf(stderr, "%s: gdb listening on 127.0.0.1:%u\n", emu_prog, (unsigned)ntohs(address.sin_port));
    fflush(stderr);
    int fd;
    do {
        fd = accept(listener, NULL, NULL);
    } while (fd < 0 && errno == EINTR);
    int reason = errno;
    close(listener); /* one client per run */
    if (fd < 0) {
        fprintf(stderr, "%s: gdb: accept failed: %s\n", emu_prog, strerror(reason));
        return -1;
    }
    /* Every exchange is one small packet and an ack: without this, Nagle's algorithm against
     * delayed acks adds tens of milliseconds to each step. */
    setsockopt(fd, IPPROTO_TCP, TCP_NODELAY, &one, sizeof one);
    return fd;
}
