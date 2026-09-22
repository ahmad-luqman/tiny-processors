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
RV32_CAPSTONE_OBJS := build/rv32/gpu.o build/rv32/gpu_ref.o build/rv32/gpu_demo.o  build/rv32/gfx_text.o build/rv32/capstone.o build/rv32/runtime.o build/rv32/tetris_game.o build/rv32/pong_game.o build/rv32/gfx.o build/rv32/digit_ui.o build/rv32/digit_model.o build/rv32/digit_hw.o build/rv32/simd4.o $(RV32_COMMON_OBJS)
RV32_HEADERS += programs/rv32/gpu.h programs/rv32/gpu_demo.h  programs/rv32/runtime.h programs/rv32/tetris_game.h programs/rv32/simd4.h programs/rv32/digit_model.h programs/rv32/digit_hw.h programs/rv32/digit_ui.h

# N1's generated headers. Defined here, above every rule that names them: make
# expands a prerequisite when it reads the rule, so a variable defined further
# down the file expands to nothing and silently drops the dependency.
# Every module a generator imports is a prerequisite: the weight layout depends on
# the kernel depth and the lane count, so changing dense4.py or rv32_digit_kernels.py
# has to rebuild the weights and not just the program bank. These must be defined
# before the rules that reference them, or they expand to nothing.
RV32_MNIST := third_party/mnist/t10k-images-idx3-ubyte.gz third_party/mnist/t10k-labels-idx1-ubyte.gz
RV32_DIGIT_KERNEL_DEPS := tools/rv32_digit_kernels.py programs/simd4/dense4.py tools/simd4_model.py
RV32_DIGIT_MODEL_DEPS := tools/rv32_digit_model.py tools/digit_ref.py tools/digit_data.py \
                         programs/rv32/digit_model.json $(RV32_DIGIT_KERNEL_DEPS)
RV32_DIGIT_GENERATED := build/rv32/digit_kernels.h build/rv32/digit_shape.h build/rv32/digit_weights.h build/rv32/digit_check.h

RV32_IMAGES := selfcheck diag pong capstone
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
FP32_RTL := rtl/fp32/fp32.v
FP32_HEADERS := rtl/fp32/fp32_states.vh rtl/fp32/fp32_ops.vh
SOFTFLOAT_OBJ := $(patsubst third_party/softfloat/%.c,build/fp32/softfloat/%.o,$(wildcard third_party/softfloat/*.c))
FP32_REF_OBJ := build/fp32/fp32_ref.o build/fp32/rv32_fp.o $(SOFTFLOAT_OBJ)
FP32_REF_FLAGS := -std=c11 -O2 -Wall -Wextra -Werror -DSOFTFLOAT_FAST_INT64 -DSOFTFLOAT_ROUND_ODD -DINLINE_LEVEL=5 -Ithird_party/softfloat -Ithird_party/softfloat/include
FP32_REF_HEADERS := $(wildcard third_party/softfloat/*.h third_party/softfloat/include/*.h)
FP32_REF := build/fp32/reference
FP32_TB := tests/fp32_tb.sv
FP32_PROTOCOL_TB := tests/fp32_protocol_tb.sv
FP32_VERILATOR := build/verilator-fp32/fp32_sim
FP32_RUN = $(PYTHON) tools/fp32_vectors.py
FP32_SEED ?= 20260921
FP32_RANDOM ?= 100
RV32_FP_OBJ := build/fp32/rv32_fp.o $(SOFTFLOAT_OBJ)
RV32EMU := build/rv32/rv32emu
RV32EMU_CFLAGS := -std=c11 -O2 -Wall -Wextra -Werror
RV32EMU_CORE := tools/rv32_gpu.c tools/rv32_gpu.h programs/rv32/gpu.h  tools/rv32emu_core.c tools/rv32emu_core.h tools/rv32_fp.h tools/rv32_simd4.c tools/rv32_simd4.h
RV32WIN := build/rv32/rv32win
# Recursive `=`: pkg-config runs only where the window is built, so a machine without SDL3 still runs every test.
SDL3_CFLAGS = $(shell pkg-config --cflags sdl3 2>/dev/null)
SDL3_LIBS = $(shell pkg-config --libs sdl3 2>/dev/null)
RV32_RTL := rtl/rv32/rv32_fregfile.v rtl/rv32/rv32_fdecode.v $(FP32_RTL) rtl/rv32/rv32_regfile.v rtl/rv32/rv32_alu.v rtl/rv32/rv32_decode.v rtl/rv32/rv32.v
RV32_SOC_RTL := $(RV32_RTL) rtl/rv32/rv32_bus.v rtl/rv32/rv32_ram.v rtl/rv32/rv32_console.v rtl/rv32/rv32_done.v rtl/rv32/rv32_timer.v rtl/rv32/rv32_input.v rtl/rv32/rv32_display.v rtl/rv32/rv32_soc.v rtl/rv32/rv32_gpu.v rtl/rv32/rv32_simd4.v $(SIMD4_RTL)
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
	$(PYTHON) -m unittest discover -s tests -p 'test_simd4_*.py' -v

test-simd4: $(SIMD4_ICARUS) test-simd4-model
	$(PYTHON) -m tools.simd4_run --simulator icarus

test-simd4-verilator: $(SIMD4_VERILATOR) test-simd4-model
	$(PYTHON) -m tools.simd4_run --simulator verilator
	$(PYTHON) -m tools.simd4_run --simulator verilator --mode waves

sim-simd4: test-simd4
	$(PYTHON) -m tools.simd4_run --mode waves

waves-simd4: sim-simd4
	@echo "Open build/simd4/icarus/vector-wave.vcd, stalled-wave.vcd, matrix-wave.vcd or overflow-wave.vcd in Surfer: https://app.surfer-project.org/"

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

build/rv32/capstone.elf: $(RV32_CAPSTONE_OBJS) programs/rv32/link.ld
	$(RV32_CC) $(RV32_LDFLAGS) -Wl,-Map,$(@:.elf=.map) -o $@ $(RV32_CAPSTONE_OBJS)

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

	$(PYTHON) tools/rv32_image.py build/rv32/capstone.elf --listing build/rv32/capstone.lst --bin build/rv32/capstone.bin --hex build/rv32/capstone.hex

run-rv32-qemu: check-rv32-image
	$(PYTHON) tools/rv32_run_qemu.py build/rv32/selfcheck.elf --qemu $(QEMU_RV32) --timeout 20 --transcript build/rv32/selfcheck.transcript --qemu-log build/rv32/qemu.log --expect-hex $(RV32_SELFCHECK_HEX)

test-rv32-tools:
	$(PYTHON) -m unittest discover -s tests -p 'test_rv32_tools.py' -v

test-rv32-rt:
	$(PYTHON) -m unittest discover -s tests -p 'test_rv32_rt.py' -v

$(RV32EMU): tools/rv32emu.c $(RV32EMU_CORE) $(RV32_FP_OBJ) | build/rv32
	$(HOST_CC) $(RV32EMU_CFLAGS) -o $@ tools/rv32emu.c tools/rv32emu_core.c tools/rv32_simd4.c tools/rv32_gpu.c $(RV32_FP_OBJ)

build-rv32-emu: toolchain-rv32-emu $(RV32EMU)

toolchain-rv32-win: toolchain-rv32-emu
	@command -v pkg-config >/dev/null || { echo "missing pkg-config (brew install pkg-config)"; exit 1; }
	@pkg-config --exists sdl3 || { echo "missing SDL3 (brew install sdl3)"; exit 1; }
	@echo "SDL3 $$(pkg-config --modversion sdl3)"

# The toolchain check is a prerequisite of the binary, so every target that needs the window says
# what to install rather than failing on a missing header.
$(RV32WIN): tools/rv32win.c $(RV32EMU_CORE) $(RV32_FP_OBJ) | build/rv32 toolchain-rv32-win
	$(HOST_CC) $(RV32EMU_CFLAGS) $(SDL3_CFLAGS) -o $@ tools/rv32win.c tools/rv32emu_core.c tools/rv32_simd4.c tools/rv32_gpu.c $(RV32_FP_OBJ) $(SDL3_LIBS)

build-rv32-win: toolchain-rv32-win $(RV32WIN)

# The window's tests run it under SDL's dummy video driver. SDL3 is a prerequisite of test-rv32, as
# LLVM and the simulators are: the toolchain check fails with the install hint rather than letting the
# tests skip.
test-rv32-win: toolchain-rv32-win check-rv32-image
	HOST_CC=$(HOST_CC) $(PYTHON) -m unittest discover -s tests -p 'test_rv32_win.py' -v

# The images are prerequisites so the diagnostic and Pong tests run rather than skip.
test-rv32-emu: check-rv32-image
	HOST_CC=$(HOST_CC) $(PYTHON) -m unittest discover -s tests -p 'test_rv32_emu.py' -v

run-rv32-emu: check-rv32-image $(RV32EMU)
	$(PYTHON) tools/rv32_run_emu.py build/rv32/selfcheck.bin --emulator $(RV32EMU) --transcript build/rv32/selfcheck.emu.transcript --trace build/rv32/selfcheck.trace --state build/rv32/selfcheck.state --expect-hex $(RV32_SELFCHECK_HEX)

trace-rv32-emu: run-rv32-emu
	@echo "trace: build/rv32/selfcheck.trace ($$(wc -l < build/rv32/selfcheck.trace | tr -d ' ') lines); state: build/rv32/selfcheck.state"
	@head -20 build/rv32/selfcheck.trace

diff-rv32-qemu: run-rv32-emu
	$(PYTHON) tools/rv32_diff_qemu.py build/rv32/selfcheck.elf build/rv32/selfcheck.trace --qemu $(QEMU_RV32) --log build/rv32/qemu-exec.log

$(RV32_TB_VVP): $(RV32_SOC_RTL) $(FP32_HEADERS) $(RV32_TB) | build/rv32
	iverilog -Irtl/fp32 -g2012 -Wall -s rv32_tb -o $@ $(RV32_TB) $(RV32_SOC_RTL)

$(RV32_TB_VERILATOR): $(RV32_SOC_RTL) $(FP32_HEADERS) $(RV32_TB) | build
	verilator -Irtl/fp32 --binary --timing --trace --top-module rv32_tb --Mdir build/verilator-rv32 -o rv32_sim $(RV32_TB) $(RV32_SOC_RTL)

build-rv32-rtl: $(RV32_TB_VVP)

test-rv32-rtl: check-rv32-image
	HOST_CC=$(HOST_CC) $(PYTHON) -m unittest discover -s tests -p 'test_rv32_rtl.py' -v

test-rv32-rtl-verilator: check-rv32-image $(RV32_TB_VERILATOR)
	HOST_CC=$(HOST_CC) RV32_RTL_SIM=$(RV32_TB_VERILATOR) $(PYTHON) -m unittest discover -s tests -p 'test_rv32_rtl.py' -v

lint-rv32:
	verilator -Irtl/fp32 --lint-only --Wall --language 1364-2005 --top-module rv32 $(RV32_RTL)

synth-rv32: | build
	yosys -Q -T -l build/rv32-synth.log -p 'read_verilog -Irtl/fp32 $(RV32_RTL); synth -top rv32; check -assert; select -assert-none t:*LATCH*; stat; write_json build/rv32.json'

lint-rv32-soc:
	verilator -Irtl/fp32 --lint-only --Wall --language 1364-2005 --top-module rv32_soc $(RV32_SOC_RTL)

# The memories are shrunk to 64 words so the count measures the decoder and
# the devices; the core's own count is synth-rv32's.
synth-rv32-soc: | build
	yosys -Q -T -l build/rv32-soc-synth.log -p 'read_verilog -Irtl/fp32 $(RV32_SOC_RTL); chparam -set RAM_WORDS 64 -set FB_WORDS 64 rv32_soc; synth -top rv32_soc; check -assert; select -assert-none t:*LATCH*; stat; write_json build/rv32-soc.json'

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
	@echo "W/S and UP/DOWN move, SPACE serves, P pauses, R restarts; end with Q (closing the window reports halt=stopped, status 2)."
	@echo "The session is recorded to build/rv32/pong.recorded.input; replay it with rv32emu --input or rv32win --input."
	$(RV32WIN) --image build/rv32/pong.bin --scale 3 --record build/rv32/pong.recorded.input

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

# M7: one image, the same script/checkpoints on the native model and both machines.
RV32_CAPSTONE_HEX := ea60197e
RV32_CAPSTONE_ARGS := --image build/rv32/capstone.bin --input programs/rv32/capstone.input --expect-last-line "PASS $(RV32_CAPSTONE_HEX)" --expect-checkpoints programs/rv32/capstone.expected --timeout 300
.PHONY: test-rv32-capstone run-rv32-capstone run-rv32-capstone-emu run-rv32-capstone-rtl run-rv32-capstone-rtl-verilator frames-rv32-capstone disasm-rv32-capstone

# The native model now compiles digit_ui.c and digit_model.c too, which include
# the generated headers.
test-rv32-capstone: $(RV32_DIGIT_GENERATED)
	HOST_CC=$(HOST_CC) $(PYTHON) -m unittest discover -s tests -p 'test_rv32_capstone.py' -v

run-rv32-capstone: check-rv32-image $(RV32WIN)
	@echo "UP/DOWN select, ENTER plays, ESC returns, Q quits. Both games: P pauses, R restarts."
	$(RV32WIN) --image build/rv32/capstone.bin --scale 3 --record build/rv32/capstone.recorded.input --checkpoints build/rv32/capstone.recorded.checkpoints

run-rv32-capstone-emu: check-rv32-image $(RV32EMU)
	$(PYTHON) -m tools.rv32_rtl $(RV32_CAPSTONE_ARGS) --backend emulator --emulator $(RV32EMU) --out build/rv32/emu

run-rv32-capstone-rtl: check-rv32-image $(RV32_TB_VVP) $(RV32EMU)
	$(PYTHON) -m tools.rv32_rtl $(RV32_CAPSTONE_ARGS) --max-cycles 20000000 --emulator $(RV32EMU) --simulator $(RV32_TB_VVP) --out build/rv32/rtl

run-rv32-capstone-rtl-verilator: check-rv32-image $(RV32_TB_VERILATOR) $(RV32EMU)
	$(PYTHON) -m tools.rv32_rtl $(RV32_CAPSTONE_ARGS) --max-cycles 20000000 --emulator $(RV32EMU) --simulator $(RV32_TB_VERILATOR) --stall 1 --out build/rv32/rtl-verilator

frames-rv32-capstone: check-rv32-image $(RV32EMU)
	$(PYTHON) -m tools.rv32_rtl $(RV32_CAPSTONE_ARGS) --backend emulator --frames build/rv32/capstone-frames --emulator $(RV32EMU) --out build/rv32/emu

disasm-rv32-capstone: firmware-rv32
	cat build/rv32/capstone.lst

test-rv32: test-rv32-capstone run-rv32-capstone-emu run-rv32-capstone-rtl run-rv32-capstone-rtl-verilator

# Compile the same directed C checks as a standalone sanitized executable.
.PHONY: test-rv32-capstone-sanitize
test-rv32-capstone-sanitize: build/rv32/digit_shape.h build/rv32/digit_weights.h | build/rv32
	mkdir -p build/rv32/host
	$(HOST_CC) -std=c11 -O1 -g -Wall -Wextra -Werror -fno-builtin -fsanitize=address,undefined -fno-sanitize-recover=undefined -fno-omit-frame-pointer -DRV32_NATIVE_MAIN -Iprograms/rv32 -Ibuild/rv32 tests/rv32_capstone_native.c programs/rv32/gpu_demo.c programs/rv32/gpu_ref.c programs/rv32/runtime.c programs/rv32/tetris_game.c programs/rv32/pong_game.c programs/rv32/gfx.c programs/rv32/gfx_text.c programs/rv32/digit_ui.c programs/rv32/digit_model.c -o build/rv32/host/capstone-sanitize
	build/rv32/host/capstone-sanitize

# F1: standalone floating-point hardware with the pinned host oracle.
.PHONY: test-fp32-tools-verilator test-fp32 test-fp32-verilator test-fp32-tools lint-fp32 synth-fp32 waves-fp32 bench-fp32

build/fp32:
	mkdir -p $@

build/fp32/fp32_ref.o: tools/fp32_ref.c tools/rv32_fp.h $(FP32_REF_HEADERS) | build/fp32
	$(HOST_CC) $(FP32_REF_FLAGS) -c $< -o $@

# Upstream RISCV NaN helpers intentionally ignore payload parameters.
build/fp32/softfloat/%.o: third_party/softfloat/%.c $(FP32_REF_HEADERS) | build/fp32
	mkdir -p build/fp32/softfloat
	$(HOST_CC) $(filter-out -Werror,$(FP32_REF_FLAGS)) -Wno-unused-parameter -c $< -o $@

$(FP32_REF): $(FP32_REF_OBJ)
	$(HOST_CC) $(FP32_REF_OBJ) -o $@

build/fp32/fp32.vvp: $(FP32_RTL) $(FP32_HEADERS) $(FP32_TB) | build/fp32
	iverilog -g2012 -Wall -Irtl/fp32 -s fp32_tb -o $@ $(FP32_TB) $(FP32_RTL)

$(FP32_VERILATOR): $(FP32_RTL) $(FP32_HEADERS) $(FP32_TB)
	verilator -Irtl/fp32 --binary --timing --trace --top-module fp32_tb --Mdir build/verilator-fp32 -o fp32_sim $(FP32_TB) $(FP32_RTL)

build/fp32/protocol.vvp: $(FP32_RTL) $(FP32_HEADERS) $(FP32_PROTOCOL_TB) | build/fp32
	iverilog -g2012 -Wall -Irtl/fp32 -s fp32_protocol_tb -o $@ $(FP32_PROTOCOL_TB) $(FP32_RTL)

build/verilator-fp32-protocol/protocol_sim: $(FP32_RTL) $(FP32_HEADERS) $(FP32_PROTOCOL_TB)
	verilator -Irtl/fp32 --binary --timing --trace --top-module fp32_protocol_tb --Mdir build/verilator-fp32-protocol -o protocol_sim $(FP32_PROTOCOL_TB) $(FP32_RTL)

test-fp32-tools: $(FP32_REF) build/fp32/fp32.vvp
	$(PYTHON) -m unittest discover -s tests -p 'test_fp32.py' -v

test-fp32-tools-verilator: $(FP32_REF) $(FP32_VERILATOR)
	FP32_TEST_SIM=$(FP32_VERILATOR) $(PYTHON) -m unittest discover -s tests -p 'test_fp32.py' -v

test-fp32: $(FP32_REF) build/fp32/fp32.vvp build/fp32/protocol.vvp test-fp32-tools
	$(FP32_RUN) --seed $(FP32_SEED) --random $(FP32_RANDOM)
	$(FP32_RUN) --protocol

test-fp32-verilator: test-fp32-tools-verilator $(FP32_REF) $(FP32_VERILATOR) build/verilator-fp32-protocol/protocol_sim
	$(FP32_RUN) --simulator $(FP32_VERILATOR) --work build/fp32/verilator --seed $(FP32_SEED) --random $(FP32_RANDOM)
	$(FP32_RUN) --protocol --simulator build/verilator-fp32-protocol/protocol_sim

lint-fp32:
	verilator -Irtl/fp32 --lint-only --Wall --language 1364-2005 --top-module fp32 $(FP32_RTL)

synth-fp32: | build/fp32
	yosys -Q -T -l build/fp32/synth.log -p 'read_verilog -Irtl/fp32 $(FP32_RTL); synth -top fp32; check -assert; select -assert-none t:$$dlatch* t:$$adlatch* t:$$_DLATCH_* t:$$_DLATCHSR_*; stat; write_json build/fp32/fp32.json'

waves-fp32: $(FP32_REF) build/fp32/fp32.vvp build/fp32/protocol.vvp
	$(FP32_RUN) --anchors-only --work build/fp32/waves --wave build/fp32/arithmetic.vcd
	$(FP32_RUN) --protocol --wave build/fp32/protocol.vcd

bench-fp32: $(FP32_REF) $(FP32_VERILATOR)
	$(FP32_RUN) --simulator $(FP32_VERILATOR) --anchors-only --stats --work build/fp32/bench

build/fp32/rv32_fp.o: tools/rv32_fp.c tools/rv32_fp.h $(FP32_REF_HEADERS) | build/fp32
	$(HOST_CC) $(FP32_REF_FLAGS) -c $< -o $@

.PHONY: test-rv32-f test-rv32-f-verilator
test-rv32-f: $(RV32EMU) $(RV32_TB_VVP) $(FP32_REF)
	RV32_RTL_SIM=$(RV32_TB_VVP) $(PYTHON) -m unittest discover -s tests -p 'test_rv32_f.py' -v

test-rv32-f-verilator: $(RV32EMU) $(RV32_TB_VERILATOR) $(FP32_REF)
	RV32_RTL_SIM=$(RV32_TB_VERILATOR) $(PYTHON) -m unittest discover -s tests -p 'test_rv32_f.py' -v

# F2 float firmware keeps ILP32; every object in these directories has explicit ISA flags.
RV32_F_CFLAGS := $(filter-out -march=rv32i,$(RV32_CFLAGS)) -march=rv32if_zicsr -ffp-contract=off
RV32_SOFT_FLAGS := $(RV32_CFLAGS) -ffp-contract=off -DSOFTFLOAT_FAST_INT64 -DINLINE_LEVEL=5 -Dopts_GCC_h -Ithird_party/softfloat -Ithird_party/softfloat/include
# opts_GCC_h disables the host-only intrinsics header (including __int128);
# the unchanged generic integer primitives are used on RV32I.
RV32_SOFT_OBJ := $(patsubst third_party/softfloat/%.c,build/rv32/soft/%.o,$(wildcard third_party/softfloat/*.c))
RV32_FLOAT_HEX := c0800000
RV32_CONVERT_HEX := 4f800003
RV32_F_LDFLAGS := $(filter-out -march=rv32i,$(RV32_LDFLAGS)) -march=rv32if_zicsr
RV32_F_IMAGES := floatcheck floatconvert floatsoft
RV32_F_FILES := $(foreach image,$(RV32_F_IMAGES),$(foreach ext,elf lst bin readelf,build/rv32/$(image).$(ext)))

build/rv32/f build/rv32/soft:
	mkdir -p $@

build/rv32/f/%.o: programs/rv32/%.c $(RV32_HEADERS) | build/rv32/f
	$(RV32_CC) $(RV32_F_CFLAGS) -c $< -o $@

build/rv32/floatcheck.elf: build/rv32/f/floatcheck.o build/rv32/f/float_work.o $(RV32_COMMON_OBJS) programs/rv32/link.ld
	$(RV32_CC) $(RV32_F_LDFLAGS) -o $@ $(filter %.o,$^)

build/rv32/floatconvert.elf: build/rv32/f/floatconvert.o $(RV32_COMMON_OBJS) programs/rv32/link.ld
	$(RV32_CC) $(RV32_F_LDFLAGS) -o $@ $(filter %.o,$^)

build/rv32/soft/%.o: third_party/softfloat/%.c $(FP32_REF_HEADERS) | build/rv32/soft
	$(RV32_CC) $(filter-out -Werror,$(RV32_SOFT_FLAGS)) -Wno-unused-parameter -c $< -o $@

build/rv32/soft/softfloat_abi.o: programs/rv32/rt/softfloat_abi.c $(FP32_REF_HEADERS) | build/rv32/soft
	$(RV32_CC) $(RV32_SOFT_FLAGS) -c $< -o $@

build/rv32/soft/float_work.o: programs/rv32/float_work.c | build/rv32/soft
	$(RV32_CC) $(RV32_CFLAGS) -ffp-contract=off -c $< -o $@

build/rv32/floatsoft.elf: build/rv32/floatcheck.o build/rv32/soft/float_work.o build/rv32/soft/softfloat_abi.o $(RV32_SOFT_OBJ) $(RV32_COMMON_OBJS) programs/rv32/link.ld
	$(RV32_CC) $(RV32_LDFLAGS) -o $@ $(filter %.o,$^)

.PHONY: firmware-rv32-f check-rv32-f-image run-rv32-f-emu run-rv32-f-rtl run-rv32-f-rtl-verilator
firmware-rv32-f: $(RV32_F_FILES)
check-rv32-f-image: firmware-rv32-f
	$(PYTHON) tools/rv32_image.py build/rv32/floatcheck.elf --listing build/rv32/floatcheck.lst --bin build/rv32/floatcheck.bin --hex build/rv32/floatcheck.hex --allow-f
	$(PYTHON) tools/rv32_image.py build/rv32/floatconvert.elf --listing build/rv32/floatconvert.lst --bin build/rv32/floatconvert.bin --hex build/rv32/floatconvert.hex --allow-f
	$(PYTHON) tools/rv32_image.py build/rv32/floatsoft.elf --listing build/rv32/floatsoft.lst --bin build/rv32/floatsoft.bin --hex build/rv32/floatsoft.hex

run-rv32-f-emu: check-rv32-f-image $(RV32EMU)
	$(PYTHON) tools/rv32_run_emu.py build/rv32/floatcheck.bin --expect-hex $(RV32_FLOAT_HEX)
	$(PYTHON) tools/rv32_run_emu.py build/rv32/floatconvert.bin --expect-hex $(RV32_CONVERT_HEX)
	$(PYTHON) tools/rv32_run_emu.py build/rv32/floatsoft.bin --expect-hex $(RV32_FLOAT_HEX)

run-rv32-f-rtl: check-rv32-f-image $(RV32EMU) $(RV32_TB_VVP)
	$(PYTHON) tools/rv32_rtl.py --image build/rv32/floatcheck.bin --expect-fp-waits 5081 --expect-last-line "PASS $(RV32_FLOAT_HEX)" --out build/rv32/f-rtl
	$(PYTHON) tools/rv32_rtl.py --image build/rv32/floatconvert.bin --expect-fp-waits 89 --expect-last-line "PASS $(RV32_CONVERT_HEX)" --out build/rv32/f-rtl
	$(PYTHON) tools/rv32_rtl.py --image build/rv32/floatsoft.bin --expect-fp-waits 0 --expect-last-line "PASS $(RV32_FLOAT_HEX)" --out build/rv32/f-rtl

run-rv32-f-rtl-verilator: check-rv32-f-image $(RV32EMU) $(RV32_TB_VERILATOR)
	$(PYTHON) tools/rv32_rtl.py --image build/rv32/floatcheck.bin --expect-fp-waits 5081 --expect-last-line "PASS $(RV32_FLOAT_HEX)" --simulator $(RV32_TB_VERILATOR) --stall 1 --out build/rv32/f-verilator
	$(PYTHON) tools/rv32_rtl.py --image build/rv32/floatconvert.bin --expect-fp-waits 89 --expect-last-line "PASS $(RV32_CONVERT_HEX)" --simulator $(RV32_TB_VERILATOR) --stall 1 --out build/rv32/f-verilator
	$(PYTHON) tools/rv32_rtl.py --image build/rv32/floatsoft.bin --expect-fp-waits 0 --expect-last-line "PASS $(RV32_FLOAT_HEX)" --simulator $(RV32_TB_VERILATOR) --stall 1 --out build/rv32/f-verilator

.PHONY: test-rv32-f-tools
test-rv32-f-tools: check-rv32-f-image $(RV32EMU) $(RV32_FP_OBJ)
	$(PYTHON) -m unittest discover -s tests -p 'test_rv32_f_tools.py' -v

test-rv32: test-rv32-f test-rv32-f-verilator test-rv32-f-tools run-rv32-f-emu run-rv32-f-rtl run-rv32-f-rtl-verilator

.PHONY: waves-rv32-f bench-rv32-f run-rv32-f-soft-qemu
waves-rv32-f: $(RV32EMU) $(RV32_TB_VVP)
	$(PYTHON) tools/rv32_f_waves.py

bench-rv32-f: run-rv32-f-rtl run-rv32-f-rtl-verilator

run-rv32-f-soft-qemu: check-rv32-f-image
	$(PYTHON) tools/rv32_run_qemu.py build/rv32/floatsoft.elf --qemu $(QEMU_RV32) --timeout 20 --expect-hex $(RV32_FLOAT_HEX) --transcript build/rv32/floatsoft.qemu.transcript --qemu-log build/rv32/floatsoft.qemu.log

test-rv32: run-rv32-f-soft-qemu

# A2: guest-owned program/data windows and asynchronous completion.
RV32_SIMD4_ARGS = --image build/rv32/simdcheck.bin --compare results --compare-stores --emulator $(RV32EMU) --expect-last-line "PASS A2"
.PHONY: run-rv32-simd4-emu run-rv32-simd4-rtl run-rv32-simd4-rtl-verilator waves-rv32-simd4 check-rv32-simd4-image
build/rv32/simd4_kernels.h: tools/rv32_simd4_kernels.py programs/simd4/vector_add.py programs/simd4/matrix_mac.py tools/simd4_model.py | build/rv32
	$(PYTHON) -m tools.rv32_simd4_kernels $@

build/rv32/simdcheck.o: programs/rv32/simdcheck.c build/rv32/simd4_kernels.h $(RV32_HEADERS)
	$(RV32_CC) $(RV32_CFLAGS) -Ibuild/rv32 -c -o $@ $<

build/rv32/simdcheck.elf: build/rv32/simdcheck.o build/rv32/simd4.o $(RV32_COMMON_OBJS) programs/rv32/link.ld
	$(RV32_CC) $(RV32_LDFLAGS) -Wl,-Map,$(@:.elf=.map) -o $@ build/rv32/simdcheck.o build/rv32/simd4.o $(RV32_COMMON_OBJS)

check-rv32-simd4-image: build/rv32/simdcheck.bin build/rv32/simdcheck.lst
	$(PYTHON) tools/rv32_image.py build/rv32/simdcheck.elf --listing build/rv32/simdcheck.lst --bin build/rv32/simdcheck.bin --hex build/rv32/simdcheck.hex

run-rv32-simd4-emu: check-rv32-simd4-image $(RV32EMU)
	$(PYTHON) tools/rv32_rtl.py $(RV32_SIMD4_ARGS) --backend emulator --out build/rv32/simd4-emu

run-rv32-simd4-rtl: check-rv32-simd4-image $(RV32EMU) $(RV32_TB_VVP)
	$(PYTHON) tools/rv32_rtl.py $(RV32_SIMD4_ARGS) --simulator $(RV32_TB_VVP) --out build/rv32/simd4-icarus

run-rv32-simd4-rtl-verilator: check-rv32-simd4-image $(RV32EMU) $(RV32_TB_VERILATOR)
	$(PYTHON) tools/rv32_rtl.py $(RV32_SIMD4_ARGS) --simulator $(RV32_TB_VERILATOR) --out build/rv32/simd4-verilator

waves-rv32-simd4: check-rv32-simd4-image $(RV32EMU) $(RV32_TB_VVP)
	$(PYTHON) tools/rv32_rtl.py $(RV32_SIMD4_ARGS) --simulator $(RV32_TB_VVP) --mode waves --out build/rv32/simd4-waves

build/rv32/simd4-protocol.vvp: rtl/rv32/rv32_simd4.v $(SIMD4_RTL) tests/rv32_simd4_tb.sv | build/rv32
	iverilog -g2012 -Wall -s rv32_simd4_tb -o $@ tests/rv32_simd4_tb.sv rtl/rv32/rv32_simd4.v $(SIMD4_RTL)

build/verilator-rv32-simd4/protocol: rtl/rv32/rv32_simd4.v $(SIMD4_RTL) tests/rv32_simd4_tb.sv | build
	verilator --binary --timing --trace --top-module rv32_simd4_tb --Mdir build/verilator-rv32-simd4 -o protocol tests/rv32_simd4_tb.sv rtl/rv32/rv32_simd4.v $(SIMD4_RTL)

.PHONY: test-rv32-simd4 test-rv32-simd4-verilator
test-rv32-simd4: check-rv32-simd4-image $(RV32EMU) $(RV32_TB_VVP) build/rv32/simd4-protocol.vvp
	$(PYTHON) -m unittest discover -s tests -p 'test_rv32_simd4.py' -v

test-rv32-simd4-verilator: check-rv32-simd4-image $(RV32EMU) $(RV32_TB_VERILATOR) build/verilator-rv32-simd4/protocol
	A2_SIM=verilator $(PYTHON) -m unittest discover -s tests -p 'test_rv32_simd4.py' -v

test-rv32: test-rv32-simd4 test-rv32-simd4-verilator run-rv32-simd4-emu run-rv32-simd4-rtl run-rv32-simd4-rtl-verilator

# G1: integer rasterizer, RAM/framebuffer blits, and menu integration.
.PHONY: test-rv32-gfx test-rv32-gfx-verilator check-rv32-gfx-image run-rv32-gfx-emu run-rv32-gfx-rtl run-rv32-gfx-rtl-verilator run-rv32-gfx-menu-emu run-rv32-gfx-menu-rtl run-rv32-gfx-menu-rtl-verilator lint-rv32-gfx synth-rv32-gfx
RV32_GFX_MAX_CYCLES := 150000000
RV32_GFX_MENU_HEX := c883a14f
RV32_GFX_ARGS = --image build/rv32/gfxcheck.bin --compare results --expect-last-line "PASS G1" --emulator $(RV32EMU) --timeout 600
RV32_GFX_MENU_ARGS = --image build/rv32/capstone.bin --input programs/rv32/gfx.input --expect-checkpoints programs/rv32/gfx.expected --expect-last-line "PASS $(RV32_GFX_MENU_HEX)" --compare results --emulator $(RV32EMU) --timeout 600
build/rv32/gfxcheck.elf: build/rv32/gfxcheck.o build/rv32/gpu.o build/rv32/gpu_ref.o build/rv32/gfx.o $(RV32_COMMON_OBJS) programs/rv32/link.ld
	$(RV32_CC) $(RV32_LDFLAGS) -Wl,-Map,$(@:.elf=.map) -o $@ $(filter %.o,$^)
check-rv32-gfx-image: build/rv32/gfxcheck.bin build/rv32/gfxcheck.lst
	$(PYTHON) tools/rv32_image.py build/rv32/gfxcheck.elf --listing build/rv32/gfxcheck.lst --bin build/rv32/gfxcheck.bin --hex build/rv32/gfxcheck.hex
test-rv32-gfx: $(RV32EMU) $(RV32_TB_VVP)
	HOST_CC=$(HOST_CC) $(PYTHON) -m unittest discover -s tests -p 'test_rv32_gfx*.py' -v
test-rv32-gfx-verilator: $(RV32EMU) $(RV32_TB_VERILATOR)
	HOST_CC=$(HOST_CC) G1_SIM=verilator $(PYTHON) -m unittest discover -s tests -p 'test_rv32_gfx*.py' -v
run-rv32-gfx-emu: check-rv32-gfx-image $(RV32EMU)
	$(PYTHON) tools/rv32_rtl.py $(RV32_GFX_ARGS) --backend emulator --out build/gfx/emu
run-rv32-gfx-rtl: check-rv32-gfx-image $(RV32EMU) $(RV32_TB_VVP)
	$(PYTHON) tools/rv32_rtl.py $(RV32_GFX_ARGS) --max-cycles $(RV32_GFX_MAX_CYCLES) --simulator $(RV32_TB_VVP) --out build/gfx/icarus
run-rv32-gfx-rtl-verilator: check-rv32-gfx-image $(RV32EMU) $(RV32_TB_VERILATOR)
	$(PYTHON) tools/rv32_rtl.py $(RV32_GFX_ARGS) --max-cycles $(RV32_GFX_MAX_CYCLES) --simulator $(RV32_TB_VERILATOR) --stall 1 --gpu-stall 2 --out build/gfx/verilator
run-rv32-gfx-menu-emu: check-rv32-image $(RV32EMU)
	$(PYTHON) tools/rv32_rtl.py $(RV32_GFX_MENU_ARGS) --backend emulator --out build/gfx/menu-emu
run-rv32-gfx-menu-rtl: check-rv32-image $(RV32EMU) $(RV32_TB_VVP)
	$(PYTHON) tools/rv32_rtl.py $(RV32_GFX_MENU_ARGS) --max-cycles $(RV32_GFX_MAX_CYCLES) --simulator $(RV32_TB_VVP) --out build/gfx/menu-icarus
run-rv32-gfx-menu-rtl-verilator: check-rv32-image $(RV32EMU) $(RV32_TB_VERILATOR)
	$(PYTHON) tools/rv32_rtl.py $(RV32_GFX_MENU_ARGS) --max-cycles $(RV32_GFX_MAX_CYCLES) --simulator $(RV32_TB_VERILATOR) --seed 17 --gpu-seed 31 --out build/gfx/menu-verilator
lint-rv32-gfx:
	verilator --lint-only --Wall --language 1364-2005 --top-module rv32_gpu rtl/rv32/rv32_gpu.v
synth-rv32-gfx: | build
	yosys -Q -T -l build/gpu-synth.log -p 'read_verilog rtl/rv32/rv32_gpu.v; synth -top rv32_gpu; check -assert; select -assert-none t:*LATCH*; stat; write_json build/gpu.json'
test-rv32: test-rv32-gfx test-rv32-gfx-verilator run-rv32-gfx-emu run-rv32-gfx-rtl run-rv32-gfx-rtl-verilator run-rv32-gfx-menu-emu run-rv32-gfx-menu-rtl-verilator lint-rv32-gfx synth-rv32-gfx

.PHONY: bench-rv32-gfx waves-rv32-gfx
build/rv32/gfxbench_cpu.o: programs/rv32/gfxbench.c $(RV32_HEADERS) | build/rv32
	$(RV32_CC) $(RV32_CFLAGS) -DG1_CPU -c $< -o $@
build/rv32/gfxbench_cpu.elf: build/rv32/gfxbench_cpu.o build/rv32/gpu_ref.o build/rv32/gfx.o $(RV32_COMMON_OBJS) programs/rv32/link.ld
	$(RV32_CC) $(RV32_LDFLAGS) -o $@ $(filter %.o,$^)
build/rv32/gfxbench.elf: build/rv32/gfxbench.o build/rv32/gpu.o build/rv32/gpu_ref.o build/rv32/gfx.o $(RV32_COMMON_OBJS) programs/rv32/link.ld
	$(RV32_CC) $(RV32_LDFLAGS) -o $@ $(filter %.o,$^)
bench-rv32-gfx: build/rv32/gfxbench.bin build/rv32/gfxbench_cpu.bin $(RV32EMU) $(RV32_TB_VERILATOR)
	$(PYTHON) tools/rv32_gfx_bench.py --emulator $(RV32EMU) --simulator $(RV32_TB_VERILATOR)
waves-rv32-gfx: test-rv32-gfx
	$(PYTHON) tools/rv32_gfx_waves.py
.PHONY: test-rv32-gfx-sanitize
test-rv32-gfx-sanitize: test-rv32-gfx | build
	mkdir -p build/gfx
	$(HOST_CC) -std=c11 -O1 -g -Wall -Wextra -Werror -fsanitize=address,undefined -fno-sanitize-recover=undefined -fno-omit-frame-pointer -DG1_NATIVE_MAIN tests/rv32_gpu_native.c tools/rv32_gpu.c programs/rv32/gpu_ref.c programs/rv32/gfx.c -o build/gfx/sanitize
	build/gfx/sanitize build/gfx/commands.txt
test-rv32: test-rv32-gfx-sanitize

# N1: quantized digit inference. The model, its oracle and the vendored test set
# are checked in Python; the same arithmetic then runs as guest C on the CPU and
# on the SIMD4 accelerator, which must agree bit for bit.
# tools/digit_train.py retrains the model. It needs numpy and the network and is
# deliberately not a prerequisite of anything here.
.PHONY: test-rv32-digit accuracy-rv32-digit check-rv32-digit-image
.PHONY: run-rv32-digit-emu run-rv32-digit-rtl run-rv32-digit-rtl-verilator
# The longest N1 run is the 199-frame menu replay at 20.3 M cycles on Verilator
# with stalls; this ceiling is roughly four times that, matching G1's margin.
RV32_DIGIT_MAX_CYCLES := 80000000
# No --compare-stores: the diagnostic checks the engine's cycle relation, so it
# stores the CYCLES and STALLS counters, and those are device time. A K=49 launch
# costs 1,008 cycles on the emulator and 1,808 on RTL with two wait cycles per
# transfer, so an ordered-store comparison would fail on a correct run. Console,
# checkpoints and trap records still have to match, and the deterministic counters
# (transfers, instructions, launches) are asserted inside the guest on every backend.
RV32_DIGIT_ARGS = --image build/rv32/digitcheck.bin --compare results \
                  --expect-last-line "PASS N1" --emulator $(RV32EMU) --timeout 900
build/rv32/digit_kernels.h: $(RV32_DIGIT_KERNEL_DEPS) | build/rv32
	$(PYTHON) -m tools.rv32_digit_kernels $@
build/rv32/digit_shape.h: $(RV32_DIGIT_MODEL_DEPS) | build/rv32
	$(PYTHON) -m tools.rv32_digit_model shape $@
build/rv32/digit_weights.h: build/rv32/digit_shape.h $(RV32_DIGIT_MODEL_DEPS) | build/rv32
	$(PYTHON) -m tools.rv32_digit_model weights $@
build/rv32/digit_check.h: $(RV32_DIGIT_MODEL_DEPS) $(RV32_MNIST) | build/rv32
	$(PYTHON) -m tools.rv32_digit_model check $@
# Every object that reaches digit_model.h needs the generated weights header.
build/rv32/digit_ui.o: programs/rv32/digit_ui.c build/rv32/digit_shape.h $(RV32_HEADERS) | build/rv32
	$(RV32_CC) $(RV32_CFLAGS) -Ibuild/rv32 -c $< -o $@
build/rv32/runtime.o: programs/rv32/runtime.c build/rv32/digit_shape.h $(RV32_HEADERS) | build/rv32
	$(RV32_CC) $(RV32_CFLAGS) -Ibuild/rv32 -c $< -o $@
build/rv32/capstone.o: programs/rv32/capstone.c $(RV32_DIGIT_GENERATED) $(RV32_HEADERS) | build/rv32
	$(RV32_CC) $(RV32_CFLAGS) -Ibuild/rv32 -c $< -o $@
build/rv32/digit_model.o: programs/rv32/digit_model.c build/rv32/digit_weights.h $(RV32_HEADERS) | build/rv32
	$(RV32_CC) $(RV32_CFLAGS) -Ibuild/rv32 -c $< -o $@
build/rv32/digit_hw.o: programs/rv32/digit_hw.c build/rv32/digit_weights.h build/rv32/digit_kernels.h $(RV32_HEADERS) | build/rv32
	$(RV32_CC) $(RV32_CFLAGS) -Ibuild/rv32 -c $< -o $@
build/rv32/digitcheck.o: programs/rv32/digitcheck.c $(RV32_DIGIT_GENERATED) $(RV32_HEADERS) | build/rv32
	$(RV32_CC) $(RV32_CFLAGS) -Ibuild/rv32 -c $< -o $@
build/rv32/digitcheck.elf: build/rv32/digitcheck.o build/rv32/digit_model.o build/rv32/digit_hw.o build/rv32/simd4.o $(RV32_COMMON_OBJS) programs/rv32/link.ld
	$(RV32_CC) $(RV32_LDFLAGS) -Wl,-Map,$(@:.elf=.map) -o $@ $(filter %.o,$^)
check-rv32-digit-image: build/rv32/digitcheck.bin build/rv32/digitcheck.lst
	$(PYTHON) tools/rv32_image.py build/rv32/digitcheck.elf --listing build/rv32/digitcheck.lst --bin build/rv32/digitcheck.bin --hex build/rv32/digitcheck.hex
test-rv32-digit: $(RV32_DIGIT_GENERATED)
	HOST_CC=$(HOST_CC) $(PYTHON) -m unittest discover -s tests -p 'test_rv32_digit*.py' -v
# The RTL half of N1. Nothing in the Python suite drives a simulator, so a target
# that only reran it would pass with Verilator missing or the RTL broken. The dense
# kernels replay through the A2 fixtures, and the diagnostic runs on the stalled
# Verilator machine.
.PHONY: test-rv32-digit-verilator
test-rv32-digit-verilator: test-rv32-digit test-rv32-simd4-verilator run-rv32-digit-rtl-verilator
accuracy-rv32-digit:
	$(PYTHON) -m tools.digit_ref --count 10000
run-rv32-digit-emu: check-rv32-digit-image $(RV32EMU)
	$(PYTHON) tools/rv32_rtl.py $(RV32_DIGIT_ARGS) --backend emulator --out build/digit/emu
run-rv32-digit-rtl: check-rv32-digit-image $(RV32EMU) $(RV32_TB_VVP)
	$(PYTHON) tools/rv32_rtl.py $(RV32_DIGIT_ARGS) --max-cycles $(RV32_DIGIT_MAX_CYCLES) --simulator $(RV32_TB_VVP) --out build/digit/icarus
run-rv32-digit-rtl-verilator: check-rv32-digit-image $(RV32EMU) $(RV32_TB_VERILATOR)
	$(PYTHON) tools/rv32_rtl.py $(RV32_DIGIT_ARGS) --max-cycles $(RV32_DIGIT_MAX_CYCLES) --simulator $(RV32_TB_VERILATOR) --stall 1 --simd-stall 2 --out build/digit/verilator

# The menu session: the guest draws two digits with the keyboard and classifies
# them. Results mode, because the classification touches accelerator registers.
# The Icarus variant is available separately, as the G1 menu replay is.
.PHONY: run-rv32-digit-menu-emu run-rv32-digit-menu-rtl run-rv32-digit-menu-rtl-verilator
RV32_DIGIT_MENU_HEX := badb5523
RV32_DIGIT_MENU_ARGS = --image build/rv32/capstone.bin --input programs/rv32/digit.input \
                       --expect-checkpoints programs/rv32/digit.expected \
                       --expect-last-line "PASS $(RV32_DIGIT_MENU_HEX)" --compare results \
                       --emulator $(RV32EMU) --timeout 900
run-rv32-digit-menu-emu: check-rv32-image $(RV32EMU)
	$(PYTHON) tools/rv32_rtl.py $(RV32_DIGIT_MENU_ARGS) --backend emulator --out build/digit/menu-emu
run-rv32-digit-menu-rtl: check-rv32-image $(RV32EMU) $(RV32_TB_VVP)
	$(PYTHON) tools/rv32_rtl.py $(RV32_DIGIT_MENU_ARGS) --max-cycles $(RV32_DIGIT_MAX_CYCLES) --simulator $(RV32_TB_VVP) --out build/digit/menu-icarus
run-rv32-digit-menu-rtl-verilator: check-rv32-image $(RV32EMU) $(RV32_TB_VERILATOR)
	$(PYTHON) tools/rv32_rtl.py $(RV32_DIGIT_MENU_ARGS) --max-cycles $(RV32_DIGIT_MAX_CYCLES) --simulator $(RV32_TB_VERILATOR) --stall 1 --simd-stall 2 --out build/digit/menu-verilator
test-rv32: test-rv32-digit accuracy-rv32-digit run-rv32-digit-emu run-rv32-digit-rtl-verilator
test-rv32: run-rv32-digit-menu-emu run-rv32-digit-menu-rtl-verilator

.PHONY: bench-rv32-digit waves-rv32-digit
# Two workload sizes per variant: subtracting them cancels startup and the bank load.
build/rv32/digitbench_cpu_%.o: programs/rv32/digitbench.c $(RV32_DIGIT_GENERATED) $(RV32_HEADERS) | build/rv32
	$(RV32_CC) $(RV32_CFLAGS) -Ibuild/rv32 -DDIGIT_BENCH_CPU -DDIGIT_BENCH_COUNT=$*u -c $< -o $@
build/rv32/digitbench_hw_%.o: programs/rv32/digitbench.c $(RV32_DIGIT_GENERATED) $(RV32_HEADERS) | build/rv32
	$(RV32_CC) $(RV32_CFLAGS) -Ibuild/rv32 -DDIGIT_BENCH_COUNT=$*u -c $< -o $@
build/rv32/digitbench_%.elf: build/rv32/digitbench_%.o build/rv32/digit_model.o build/rv32/digit_hw.o build/rv32/simd4.o $(RV32_COMMON_OBJS) programs/rv32/link.ld
	$(RV32_CC) $(RV32_LDFLAGS) -o $@ $(filter %.o,$^)
RV32_DIGIT_BENCH_BINS := $(foreach v,cpu hw,$(foreach n,1 5,build/rv32/digitbench_$(v)_$(n).bin))
bench-rv32-digit: $(RV32_DIGIT_BENCH_BINS) $(RV32EMU) $(RV32_TB_VERILATOR)
	$(PYTHON) tools/rv32_digit_bench.py --emulator $(RV32EMU) --simulator $(RV32_TB_VERILATOR)
# The single-inference bench image, not the diagnostic: dumping all eight
# classifications and both recovery paths produced a five-gigabyte VCD, and one
# inference already contains every launch edge worth looking at.
waves-rv32-digit: build/rv32/digitbench_hw_1.bin $(RV32EMU) $(RV32_TB_VVP)
	$(PYTHON) tools/rv32_rtl.py --image build/rv32/digitbench_hw_1.bin --compare results \
	  --expect-last-line "bench 3791" --emulator $(RV32EMU) --timeout 900 \
	  --max-cycles $(RV32_DIGIT_MAX_CYCLES) --simulator $(RV32_TB_VVP) --mode waves --out build/digit/waves
