.DEFAULT_GOAL := test
# A recipe that fails leaves no half-written target behind: an empty listing or hex file that is
# newer than its source would otherwise be "up to date" and pass the image checks vacuously.
.DELETE_ON_ERROR:
PYTHON ?= python3

RTL := labs/01-counter/counter.v
TB := labs/01-counter/counter_tb.sv
ALU_RTL := labs/02-alu/alu.v
ALU_TB := labs/02-alu/alu_tb.sv
SAP8_RTL := rtl/sap8/sap8.v $(ALU_RTL)
SAP8_TB := tests/sap8_tb.sv
SAP8_ADD_ARGS := +program=build/sap8/add.program.hex +data=build/sap8/add.data.hex +expected=12 +instructions=6 +memory-address=240 +memory-value=7
SAP8_LOOP_ARGS := +program=build/sap8/sum_loop.program.hex +data=build/sap8/sum_loop.data.hex +expected=6 +instructions=29 +memory-address=241 +memory-value=0
SIMD4_RTL := rtl/simd4/simd4.v
SIMD4_TB := tests/simd4_tb.sv
SIMD4_ICARUS := build/simd4-1.vvp build/simd4-2.vvp build/simd4-4.vvp
SIMD4_VERILATOR := build/verilator-simd4-1/simd4_sim build/verilator-simd4-2/simd4_sim build/verilator-simd4-4/simd4_sim
RV32_LLVM ?= /opt/homebrew/opt/llvm@22/bin
RV32_CC ?= $(RV32_LLVM)/clang
RV32_LD ?= /opt/homebrew/opt/lld/bin/ld.lld
RV32_OBJDUMP ?= $(RV32_LLVM)/llvm-objdump
RV32_OBJCOPY ?= $(RV32_LLVM)/llvm-objcopy
RV32_READELF ?= $(RV32_LLVM)/llvm-readelf
RV32_NM ?= $(RV32_LLVM)/llvm-nm
QEMU_RV32 ?= qemu-system-riscv32
HOST_CC ?= cc
RV32_ARCH := --target=riscv32-unknown-elf -march=rv32i -mabi=ilp32 -mcmodel=medlow -mno-relax
RV32_CFLAGS := $(RV32_ARCH) -std=c11 -ffreestanding -fno-builtin -nostdlib -O2 -g -fno-asynchronous-unwind-tables -fno-unwind-tables -Wall -Wextra -Werror -Iprograms/rv32
RV32_LDFLAGS := $(RV32_ARCH) -nostdlib -static --ld-path=$(RV32_LD) -Wl,-T,programs/rv32/link.ld
RV32_HEADERS := programs/rv32/board.h programs/rv32/mmio.h programs/rv32/console.h programs/rv32/rt/muldiv.h programs/rv32/gfx.h programs/rv32/pong_game.h
RV32_COMMON_OBJS := build/rv32/start.o build/rv32/console.o build/rv32/muldiv.o
RV32_SELFCHECK_OBJS := build/rv32/selfcheck.o $(RV32_COMMON_OBJS)
RV32_DIAG_OBJS := build/rv32/diag.o build/rv32/trap.o $(RV32_COMMON_OBJS)
RV32_PONG_OBJS := build/rv32/pong.o build/rv32/pong_game.o build/rv32/gfx.o $(RV32_COMMON_OBJS)
RV32_IMAGES := selfcheck diag pong
RV32_IMAGE_FILES := $(foreach image,$(RV32_IMAGES),$(foreach ext,elf lst bin readelf,build/rv32/$(image).$(ext)))
RV32_SELFCHECK_HEX := 807d9fad
RV32_DIAG_HEX := 8bd87e9a
RV32_DIAG_FRAME1_HEX := ae4eb605
RV32_DIAG_FRAME2_HEX := 2acb5d85
RV32_DIAG_INPUT := programs/rv32/diag.input
RV32_PONG_HEX := 8fef54bc
RV32_PONG_INPUT := programs/rv32/pong.input
RV32_PONG_EXPECTED := programs/rv32/pong.expected
RV32_PONG_ARGS := --image build/rv32/pong.bin --input $(RV32_PONG_INPUT) --expect-last-line "PASS $(RV32_PONG_HEX)" --expect-checkpoints $(RV32_PONG_EXPECTED) --timeout 300
RV32_DIAG_ARGS := --image build/rv32/diag.bin --input $(RV32_DIAG_INPUT) --compare results --expect-last-line "PASS $(RV32_DIAG_HEX)" --expect-checkpoint "frame 1 $(RV32_DIAG_FRAME1_HEX)" --expect-checkpoint "frame 2 $(RV32_DIAG_FRAME2_HEX)"
RV32EMU := build/rv32/rv32emu
RV32EMU_CFLAGS := -std=c11 -O2 -Wall -Wextra -Werror
RV32EMU_CORE := tools/rv32emu_core.c tools/rv32emu_core.h
RV32WIN := build/rv32/rv32win
# Recursive `=`: pkg-config runs only where the window is built, so a machine without SDL3 still runs every test.
SDL3_CFLAGS = $(shell pkg-config --cflags sdl3 2>/dev/null)
SDL3_LIBS = $(shell pkg-config --libs sdl3 2>/dev/null)
RV32_RTL := rtl/rv32/rv32_regfile.v rtl/rv32/rv32_alu.v rtl/rv32/rv32_decode.v rtl/rv32/rv32.v
RV32_SOC_RTL := $(RV32_RTL) rtl/rv32/rv32_bus.v rtl/rv32/rv32_ram.v rtl/rv32/rv32_console.v rtl/rv32/rv32_done.v rtl/rv32/rv32_timer.v rtl/rv32/rv32_input.v rtl/rv32/rv32_display.v rtl/rv32/rv32_soc.v
RV32_TB := tests/rv32_tb.sv
RV32_TB_VVP := build/rv32/rv32_tb.vvp
RV32_TB_VERILATOR := build/verilator-rv32/rv32_sim

.PHONY: test sim lint synth test-verilator waves clean
.PHONY: test-alu sim-alu lint-alu synth-alu test-alu-verilator waves-alu
.PHONY: test-sap8 sim-sap8 lint-sap8 synth-sap8 test-sap8-verilator waves-sap8
.PHONY: test-sap8-assembler programs-sap8
.PHONY: test-simd4-model test-simd4 test-simd4-verilator sim-simd4 waves-simd4 bench-simd4 lint-simd4 synth-simd4
.PHONY: toolchain-rv32 firmware-rv32 check-rv32-image run-rv32-qemu test-rv32-tools test-rv32-rt test-rv32 disasm-rv32
.PHONY: run-rv32-diag-emu run-rv32-diag-rtl run-rv32-diag-rtl-verilator disasm-rv32-diag
.PHONY: toolchain-rv32-emu build-rv32-emu test-rv32-emu run-rv32-emu trace-rv32-emu diff-rv32-qemu
.PHONY: build-rv32-rtl test-rv32-rtl test-rv32-rtl-verilator run-rv32-rtl run-rv32-rtl-verilator lint-rv32 synth-rv32 waves-rv32 bench-rv32-rtl
.PHONY: lint-rv32-soc synth-rv32-soc
.PHONY: toolchain-rv32-win build-rv32-win test-rv32-win test-rv32-pong run-rv32-pong run-rv32-pong-emu frames-rv32-pong
.PHONY: run-rv32-pong-rtl run-rv32-pong-rtl-verilator disasm-rv32-pong

build:
	mkdir -p build

build/counter.vvp: $(RTL) $(TB) | build
	iverilog -g2012 -Wall -s counter_tb -o $@ $(TB) $(RTL)

test: build/counter.vvp
	vvp $<

sim: build/counter.vvp
	vvp $< +wave=build/counter.vcd

lint:
	verilator --lint-only --Wall --language 1364-2005 --top-module counter $(RTL)

synth: | build
	yosys -Q -T -l build/counter-synth.log -p 'read_verilog $(RTL); synth -top counter; check -assert; stat; write_json build/counter.json'

test-verilator: | build
	verilator --binary --timing --trace --top-module counter_tb --Mdir build/verilator -o counter_sim $(TB) $(RTL)
	./build/verilator/counter_sim +wave=build/counter-verilator.vcd

waves: sim
	@echo "Open build/counter.vcd in Surfer: https://app.surfer-project.org/"

build/alu.vvp: $(ALU_RTL) $(ALU_TB) | build
	iverilog -g2012 -Wall -s alu_tb -o $@ $(ALU_TB) $(ALU_RTL)

test-alu: build/alu.vvp
	vvp $<

sim-alu: test-alu
	vvp build/alu.vvp +examples-only +wave=build/alu.vcd

lint-alu:
	verilator --lint-only --Wall --language 1364-2005 --top-module alu $(ALU_RTL)

synth-alu: | build
	yosys -Q -T -l build/alu-synth.log -p 'read_verilog $(ALU_RTL); synth -top alu; check -assert; select -assert-none t:*DFF* t:*LATCH*; stat; write_json build/alu.json'

test-alu-verilator: | build
	verilator --binary --timing --trace --top-module alu_tb --Mdir build/verilator-alu -o alu_sim $(ALU_TB) $(ALU_RTL)
	./build/verilator-alu/alu_sim
	./build/verilator-alu/alu_sim +examples-only +wave=build/alu-verilator.vcd

waves-alu: sim-alu
	@echo "Open build/alu.vcd in Surfer: https://app.surfer-project.org/"

build/sap8.vvp: $(SAP8_RTL) $(SAP8_TB) | build
	iverilog -g2012 -Wall -s sap8_tb -o $@ $(SAP8_TB) $(SAP8_RTL)

build/sap8: | build
	mkdir -p $@

programs-sap8: | build/sap8
	$(PYTHON) tools/sap8_asm.py programs/sap8/add.asm --program build/sap8/add.program.hex --data build/sap8/add.data.hex
	$(PYTHON) tools/sap8_asm.py programs/sap8/sum_loop.asm --program build/sap8/sum_loop.program.hex --data build/sap8/sum_loop.data.hex

test-sap8-assembler:
	$(PYTHON) -m unittest discover -s tests -p 'test_sap8_asm.py' -v

test-sap8: build/sap8.vvp test-sap8-assembler programs-sap8
	vvp $<
	vvp $< $(SAP8_ADD_ARGS)
	vvp $< $(SAP8_LOOP_ARGS)

sim-sap8: test-sap8
	vvp build/sap8.vvp +examples-only +trace +wave=build/sap8.vcd > build/sap8.trace
	cat build/sap8.trace
	vvp build/sap8.vvp $(SAP8_LOOP_ARGS) +trace +wave=build/sap8-loop.vcd > build/sap8-loop.trace
	cat build/sap8-loop.trace

lint-sap8:
	verilator --lint-only --Wall --language 1364-2005 --top-module sap8 $(SAP8_RTL)

synth-sap8: | build
	yosys -Q -T -l build/sap8-synth.log -p 'read_verilog $(SAP8_RTL); synth -top sap8; check -assert; select -assert-none t:*LATCH*; stat; write_json build/sap8.json'

test-sap8-verilator: programs-sap8 | build
	verilator --binary --timing --trace --top-module sap8_tb --Mdir build/verilator-sap8 -o sap8_sim $(SAP8_TB) $(SAP8_RTL)
	./build/verilator-sap8/sap8_sim
	./build/verilator-sap8/sap8_sim +examples-only +trace +wave=build/sap8-verilator.vcd
	./build/verilator-sap8/sap8_sim $(SAP8_ADD_ARGS)
	./build/verilator-sap8/sap8_sim $(SAP8_LOOP_ARGS) +trace +wave=build/sap8-loop-verilator.vcd

waves-sap8: sim-sap8
	@echo "Open build/sap8.vcd or build/sap8-loop.vcd in Surfer: https://app.surfer-project.org/"

build/simd4-%.vvp: $(SIMD4_RTL) $(SIMD4_TB) | build
	iverilog -g2012 -Wall -s simd4_tb -Psimd4_tb.LANES=$* -o $@ $(SIMD4_TB) $(SIMD4_RTL)

build/verilator-simd4-%/simd4_sim: $(SIMD4_RTL) $(SIMD4_TB) | build
	verilator --binary --timing --trace --top-module simd4_tb -GLANES=$* --Mdir build/verilator-simd4-$* -o simd4_sim $(SIMD4_TB) $(SIMD4_RTL)

test-simd4-model:
	$(PYTHON) -m unittest discover -s tests -p 'test_simd4_model.py' -v

test-simd4: $(SIMD4_ICARUS) test-simd4-model
	$(PYTHON) -m tools.simd4_run --simulator icarus

test-simd4-verilator: $(SIMD4_VERILATOR) test-simd4-model
	$(PYTHON) -m tools.simd4_run --simulator verilator
	$(PYTHON) -m tools.simd4_run --simulator verilator --mode waves

sim-simd4: test-simd4
	$(PYTHON) -m tools.simd4_run --mode waves

waves-simd4: sim-simd4
	@echo "Open build/simd4/icarus/vector-wave.vcd or stalled-wave.vcd in Surfer: https://app.surfer-project.org/"

bench-simd4: $(SIMD4_ICARUS)
	$(PYTHON) -m tools.simd4_run --mode bench

lint-simd4:
	verilator --lint-only --Wall --language 1364-2005 --top-module simd4 -GLANES=1 $(SIMD4_RTL)
	verilator --lint-only --Wall --language 1364-2005 --top-module simd4 -GLANES=2 $(SIMD4_RTL)
	verilator --lint-only --Wall --language 1364-2005 --top-module simd4 -GLANES=4 $(SIMD4_RTL)

synth-simd4: | build
	yosys -Q -T -l build/simd4-synth.log -p 'read_verilog $(SIMD4_RTL); synth -top simd4; check -assert; select -assert-none t:*LATCH*; stat; write_json build/simd4.json'

build/rv32: | build
	mkdir -p $@

build/rv32/%.o: programs/rv32/%.c $(RV32_HEADERS) | build/rv32
	$(RV32_CC) $(RV32_CFLAGS) -c -o $@ $<

build/rv32/%.o: programs/rv32/%.S programs/rv32/board.h | build/rv32
	$(RV32_CC) $(RV32_CFLAGS) -c -o $@ $<

build/rv32/muldiv.o: programs/rv32/rt/muldiv.c programs/rv32/rt/muldiv.h | build/rv32
	$(RV32_CC) $(RV32_CFLAGS) -c -o $@ $<

build/rv32/selfcheck.elf: $(RV32_SELFCHECK_OBJS) programs/rv32/link.ld
	$(RV32_CC) $(RV32_LDFLAGS) -Wl,-Map,$(@:.elf=.map) -o $@ $(RV32_SELFCHECK_OBJS)

build/rv32/diag.elf: $(RV32_DIAG_OBJS) programs/rv32/link.ld
	$(RV32_CC) $(RV32_LDFLAGS) -Wl,-Map,$(@:.elf=.map) -o $@ $(RV32_DIAG_OBJS)

build/rv32/pong.elf: $(RV32_PONG_OBJS) programs/rv32/link.ld
	$(RV32_CC) $(RV32_LDFLAGS) -Wl,-Map,$(@:.elf=.map) -o $@ $(RV32_PONG_OBJS)

build/rv32/%.lst: build/rv32/%.elf
	$(RV32_OBJDUMP) -d -S $< > $@

build/rv32/%.bin: build/rv32/%.elf
	$(RV32_OBJCOPY) -O binary $< $@

build/rv32/%.readelf: build/rv32/%.elf
	$(RV32_READELF) -h -l -S -s -A $< > $@

toolchain-rv32:
	@for tool in $(RV32_CC) $(RV32_OBJDUMP) $(RV32_OBJCOPY) $(RV32_READELF) $(RV32_NM); do \
		test -x $$tool || { echo "missing $$tool (brew install llvm@22)"; exit 1; }; done
	@test -x $(RV32_LD) || { echo "missing $(RV32_LD) (brew install lld)"; exit 1; }
	@command -v $(QEMU_RV32) >/dev/null || { echo "missing $(QEMU_RV32) (brew install qemu)"; exit 1; }
	@$(RV32_CC) --version | head -1
	@$(RV32_LD) --version
	@$(QEMU_RV32) --version | head -1

toolchain-rv32-emu:
	@command -v $(HOST_CC) >/dev/null || { echo "missing $(HOST_CC) (xcode-select --install)"; exit 1; }
	@$(HOST_CC) --version | head -1

firmware-rv32: toolchain-rv32 $(RV32_IMAGE_FILES)

# The diagnostic installs a trap handler, so its listing may use the CSR instructions and mret.
check-rv32-image: firmware-rv32
	$(PYTHON) tools/rv32_image.py build/rv32/selfcheck.elf --listing build/rv32/selfcheck.lst --bin build/rv32/selfcheck.bin --hex build/rv32/selfcheck.hex
	$(PYTHON) tools/rv32_image.py build/rv32/diag.elf --listing build/rv32/diag.lst --bin build/rv32/diag.bin --hex build/rv32/diag.hex --allow-privileged
	$(PYTHON) tools/rv32_image.py build/rv32/pong.elf --listing build/rv32/pong.lst --bin build/rv32/pong.bin --hex build/rv32/pong.hex

run-rv32-qemu: check-rv32-image
	$(PYTHON) tools/rv32_run_qemu.py build/rv32/selfcheck.elf --qemu $(QEMU_RV32) --timeout 20 --transcript build/rv32/selfcheck.transcript --qemu-log build/rv32/qemu.log --expect-hex $(RV32_SELFCHECK_HEX)

test-rv32-tools:
	$(PYTHON) -m unittest discover -s tests -p 'test_rv32_tools.py' -v

test-rv32-rt:
	$(PYTHON) -m unittest discover -s tests -p 'test_rv32_rt.py' -v

$(RV32EMU): tools/rv32emu.c $(RV32EMU_CORE) | build/rv32
	$(HOST_CC) $(RV32EMU_CFLAGS) -o $@ tools/rv32emu.c tools/rv32emu_core.c

build-rv32-emu: toolchain-rv32-emu $(RV32EMU)

toolchain-rv32-win: toolchain-rv32-emu
	@pkg-config --exists sdl3 || { echo "missing SDL3 (brew install sdl3)"; exit 1; }
	@echo "SDL3 $$(pkg-config --modversion sdl3)"

$(RV32WIN): tools/rv32win.c $(RV32EMU_CORE) | build/rv32
	$(HOST_CC) $(RV32EMU_CFLAGS) $(SDL3_CFLAGS) -o $@ tools/rv32win.c tools/rv32emu_core.c $(SDL3_LIBS)

build-rv32-win: toolchain-rv32-win $(RV32WIN)

# The window's tests run it under SDL's dummy video driver; they skip when SDL3 is not installed.
test-rv32-win: check-rv32-image
	HOST_CC=$(HOST_CC) $(PYTHON) -m unittest discover -s tests -p 'test_rv32_win.py' -v

# The image is a prerequisite so the diagnostic test runs rather than skips.
test-rv32-emu: check-rv32-image
	HOST_CC=$(HOST_CC) $(PYTHON) -m unittest discover -s tests -p 'test_rv32_emu.py' -v

run-rv32-emu: check-rv32-image $(RV32EMU)
	$(PYTHON) tools/rv32_run_emu.py build/rv32/selfcheck.bin --emulator $(RV32EMU) --transcript build/rv32/selfcheck.emu.transcript --trace build/rv32/selfcheck.trace --state build/rv32/selfcheck.state --expect-hex $(RV32_SELFCHECK_HEX)

trace-rv32-emu: run-rv32-emu
	@echo "trace: build/rv32/selfcheck.trace ($$(wc -l < build/rv32/selfcheck.trace | tr -d ' ') lines); state: build/rv32/selfcheck.state"
	@head -20 build/rv32/selfcheck.trace

diff-rv32-qemu: run-rv32-emu
	$(PYTHON) tools/rv32_diff_qemu.py build/rv32/selfcheck.elf build/rv32/selfcheck.trace --qemu $(QEMU_RV32) --log build/rv32/qemu-exec.log

$(RV32_TB_VVP): $(RV32_SOC_RTL) $(RV32_TB) | build/rv32
	iverilog -g2012 -Wall -s rv32_tb -o $@ $(RV32_TB) $(RV32_SOC_RTL)

$(RV32_TB_VERILATOR): $(RV32_SOC_RTL) $(RV32_TB) | build
	verilator --binary --timing --trace --top-module rv32_tb --Mdir build/verilator-rv32 -o rv32_sim $(RV32_TB) $(RV32_SOC_RTL)

build-rv32-rtl: $(RV32_TB_VVP)

test-rv32-rtl: check-rv32-image
	HOST_CC=$(HOST_CC) $(PYTHON) -m unittest discover -s tests -p 'test_rv32_rtl.py' -v

test-rv32-rtl-verilator: check-rv32-image $(RV32_TB_VERILATOR)
	HOST_CC=$(HOST_CC) RV32_RTL_SIM=$(RV32_TB_VERILATOR) $(PYTHON) -m unittest discover -s tests -p 'test_rv32_rtl.py' -v

lint-rv32:
	verilator --lint-only --Wall --language 1364-2005 --top-module rv32 $(RV32_RTL)

synth-rv32: | build
	yosys -Q -T -l build/rv32-synth.log -p 'read_verilog $(RV32_RTL); synth -top rv32; check -assert; select -assert-none t:*LATCH*; stat; write_json build/rv32.json'

lint-rv32-soc:
	verilator --lint-only --Wall --language 1364-2005 --top-module rv32_soc $(RV32_SOC_RTL)

# The memories are shrunk to 64 words so the count measures the decoder and
# the devices; the core's own count is synth-rv32's.
synth-rv32-soc: | build
	yosys -Q -T -l build/rv32-soc-synth.log -p 'read_verilog $(RV32_SOC_RTL); chparam -set RAM_WORDS 64 -set FB_WORDS 64 rv32_soc; synth -top rv32_soc; check -assert; select -assert-none t:*LATCH*; stat; write_json build/rv32-soc.json'

waves-rv32: $(RV32_TB_VVP) $(RV32EMU)
	$(PYTHON) -m tools.rv32_rtl --mode waves --program loop --emulator $(RV32EMU) --simulator $(RV32_TB_VVP) --out build/rv32/rtl
	$(PYTHON) -m tools.rv32_rtl --mode waves --program full --emulator $(RV32EMU) --simulator $(RV32_TB_VVP) --out build/rv32/rtl
	$(PYTHON) -m tools.rv32_rtl --mode waves --program devices --stall 0 --emulator $(RV32EMU) --simulator $(RV32_TB_VVP) --out build/rv32/rtl
	@echo "Open build/rv32/rtl/loop.vcd, full.vcd, or devices.vcd in Surfer: https://app.surfer-project.org/"

run-rv32-rtl: check-rv32-image $(RV32_TB_VVP) $(RV32EMU)
	$(PYTHON) -m tools.rv32_rtl --image build/rv32/selfcheck.bin --expect-console "PASS $(RV32_SELFCHECK_HEX)" --emulator $(RV32EMU) --simulator $(RV32_TB_VVP) --out build/rv32/rtl

# Each runner target has its own output directory, so `make -j` cannot interleave two runs' traces.
run-rv32-rtl-verilator: check-rv32-image $(RV32_TB_VERILATOR) $(RV32EMU)
	$(PYTHON) -m tools.rv32_rtl --image build/rv32/selfcheck.bin --expect-console "PASS $(RV32_SELFCHECK_HEX)" --emulator $(RV32EMU) --simulator $(RV32_TB_VERILATOR) --out build/rv32/rtl-verilator --stall 1

bench-rv32-rtl: $(RV32_TB_VVP) $(RV32EMU)
	$(PYTHON) -m tools.rv32_rtl --mode bench --emulator $(RV32EMU) --simulator $(RV32_TB_VVP) --out build/rv32/rtl

# The device diagnostic reads the timer, so the two backends are compared at the results level
# (console, outcome, checkpoints, and the trap records in order), not trace for trace
# (docs/rv32.md, "Device time").
run-rv32-diag-emu: check-rv32-image $(RV32EMU)
	$(PYTHON) -m tools.rv32_rtl $(RV32_DIAG_ARGS) --backend emulator --frames build/rv32/frames --emulator $(RV32EMU) --out build/rv32/emu

run-rv32-diag-rtl: check-rv32-image $(RV32_TB_VVP) $(RV32EMU)
	$(PYTHON) -m tools.rv32_rtl $(RV32_DIAG_ARGS) --emulator $(RV32EMU) --simulator $(RV32_TB_VVP) --out build/rv32/rtl

run-rv32-diag-rtl-verilator: check-rv32-image $(RV32_TB_VERILATOR) $(RV32EMU)
	$(PYTHON) -m tools.rv32_rtl $(RV32_DIAG_ARGS) --emulator $(RV32EMU) --simulator $(RV32_TB_VERILATOR) --stall 1 --out build/rv32/rtl-verilator

# Pong never reads the timer, so its scripted session is compared trace for trace on the RTL, and
# the 200 checkpoints of programs/rv32/pong.expected come from the same C run natively
# (tools/rv32_pong_native.py --write). The window target is interactive and not part of test-rv32.
test-rv32-pong:
	HOST_CC=$(HOST_CC) $(PYTHON) -m unittest discover -s tests -p 'test_rv32_pong.py' -v

run-rv32-pong: check-rv32-image $(RV32WIN)
	$(RV32WIN) --image build/rv32/pong.bin --scale 3 --record build/rv32/pong.recorded.input
	@echo "recorded: build/rv32/pong.recorded.input (replay it with rv32emu --input or rv32win --input)"

run-rv32-pong-emu: check-rv32-image $(RV32EMU)
	$(PYTHON) -m tools.rv32_rtl $(RV32_PONG_ARGS) --backend emulator --emulator $(RV32EMU) --out build/rv32/emu

frames-rv32-pong: check-rv32-image $(RV32EMU)
	$(PYTHON) -m tools.rv32_rtl $(RV32_PONG_ARGS) --backend emulator --frames build/rv32/pong-frames --emulator $(RV32EMU) --out build/rv32/emu
	@echo "frames: build/rv32/pong-frames/frame-NNNN.ppm"

run-rv32-pong-rtl: check-rv32-image $(RV32_TB_VVP) $(RV32EMU)
	$(PYTHON) -m tools.rv32_rtl $(RV32_PONG_ARGS) --emulator $(RV32EMU) --simulator $(RV32_TB_VVP) --out build/rv32/rtl

run-rv32-pong-rtl-verilator: check-rv32-image $(RV32_TB_VERILATOR) $(RV32EMU)
	$(PYTHON) -m tools.rv32_rtl $(RV32_PONG_ARGS) --emulator $(RV32EMU) --simulator $(RV32_TB_VERILATOR) --stall 1 --out build/rv32/rtl-verilator

test-rv32: test-rv32-tools test-rv32-rt test-rv32-pong run-rv32-qemu test-rv32-emu test-rv32-win run-rv32-emu diff-rv32-qemu run-rv32-diag-emu run-rv32-pong-emu test-rv32-rtl test-rv32-rtl-verilator run-rv32-rtl run-rv32-rtl-verilator run-rv32-diag-rtl run-rv32-diag-rtl-verilator run-rv32-pong-rtl run-rv32-pong-rtl-verilator lint-rv32 lint-rv32-soc synth-rv32 synth-rv32-soc

disasm-rv32: firmware-rv32
	cat build/rv32/selfcheck.lst

disasm-rv32-diag: firmware-rv32
	cat build/rv32/diag.lst

disasm-rv32-pong: firmware-rv32
	cat build/rv32/pong.lst

clean:
	rm -rf build
