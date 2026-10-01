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
# Icarus interprets the RTL, so its runs take minutes (the capstone about 510 s, the OS console
# session about 305 s and gfxcheck about 670 s on an Apple Silicon Mac, and about twice that in a
# Linux container). Each run is already bounded by --max-cycles or the testbench's 10M default, so
# the Icarus recipes give the simulator alone (--rtl-timeout) this wall-clock limit to catch a hang;
# the emulator reference keeps the target's --timeout. Override it for a slow host.
RV32_ICARUS_TIMEOUT ?= 3600
# Issue #26 (docs/rv32-testing.md): RV32_TIMING=1 runs every recipe line through
# tools/rv32_recipe_shell.py. It prints each line's output in one block, so `make -j` logs stay
# readable on GNU Make 3.81, which has no --output-sync. It also appends the line's timing to
# RV32_TIMING_LOG. A $(shell ...) evaluated while a recipe expands would go through it too, with that
# recipe's name, and capture its banners; the SDL3 flags below are evaluated early for that reason.
ifdef RV32_TIMING
RV32_TIMING_LOG ?= build/rv32-timing.jsonl
export RV32_TIMING_LOG
SHELL = $(PYTHON) tools/rv32_recipe_shell.py $@
MAKEFLAGS += -s
endif
HOST_CC ?= cc
RV32_ARCH := --target=riscv32-unknown-elf -march=rv32i -mabi=ilp32 -mcmodel=medlow -mno-relax
RV32_CFLAGS := $(RV32_ARCH) -std=c11 -ffreestanding -fno-builtin -nostdlib -O2 -g -fno-asynchronous-unwind-tables -fno-unwind-tables -Wall -Wextra -Werror -Iprograms/rv32
RV32_LDFLAGS := $(RV32_ARCH) -nostdlib -static --ld-path=$(RV32_LD) -Wl,-T,programs/rv32/link.ld
RV32_HEADERS := programs/rv32/board.h programs/rv32/mmio.h programs/rv32/console.h programs/rv32/rt/muldiv.h programs/rv32/gfx.h programs/rv32/pong_game.h
RV32_COMMON_OBJS := build/rv32/start.o build/rv32/console.o build/rv32/muldiv.o
RV32_SELFCHECK_OBJS := build/rv32/selfcheck.o $(RV32_COMMON_OBJS)
RV32_DIAG_OBJS := build/rv32/diag.o build/rv32/trap.o $(RV32_COMMON_OBJS)
RV32_PONG_OBJS := build/rv32/pong.o build/rv32/pong_game.o build/rv32/gfx.o $(RV32_COMMON_OBJS)
RV32_CAPSTONE_OBJS := build/rv32/gpu.o build/rv32/gpu_ref.o build/rv32/gpu_demo.o  build/rv32/gfx_text.o build/rv32/capstone.o build/rv32/runtime.o build/rv32/tetris_game.o build/rv32/pong_game.o build/rv32/gfx.o build/rv32/digit_ui.o build/rv32/digit_model.o build/rv32/digit_hw.o build/rv32/digit_weights.o build/rv32/simd4.o build/rv32/g3d.o build/rv32/g3d_ref.o build/rv32/g3d_demo.o $(RV32_COMMON_OBJS)
RV32_HEADERS += programs/rv32/g3d.h programs/rv32/g3d_demo.h programs/rv32/gpu.h programs/rv32/gpu_demo.h  programs/rv32/runtime.h programs/rv32/tetris_game.h programs/rv32/simd4.h programs/rv32/digit_model.h programs/rv32/digit_hw.h programs/rv32/digit_ui.h

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
RV32_DIGIT_GENERATED := build/rv32/digit_kernels.h build/rv32/digit_shape.h build/rv32/digit_weights.h build/rv32/digit_weights.c build/rv32/digit_check.h
# G2's headers come from the Python oracle, so they depend on every module the
# generator imports: a change to the model or the shaders must regenerate them.
RV32_G3D_DEPS := tools/rv32_g3d_header.py tools/rv32_g3d_model.py tools/rv32_g3d_scene.py
RV32_G3D_GENERATED := build/rv32/g3d_shaders.h build/rv32/g3d_scenes.h

RV32_IMAGES := selfcheck diag pong capstone
RV32_IMAGE_FILES := $(foreach image,$(RV32_IMAGES),$(foreach ext,elf lst bin readelf,build/rv32/$(image).$(ext)))
RV32_SELFCHECK_HEX := 807d9fad
RV32_DIAG_HEX := efd4ec82
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
RV32EMU_CORE := tools/rv32_gpu.c tools/rv32_gpu.h programs/rv32/gpu.h tools/rv32_g3d.c tools/rv32_g3d.h programs/rv32/g3d.h  tools/rv32emu_core.c tools/rv32emu_core.h tools/rv32_dtb.h tools/rv32_fp.h tools/rv32_simd4.c tools/rv32_simd4.h
RV32WIN := build/rv32/rv32win
# Recursive `=`: pkg-config runs only where the window is built, so a machine without SDL3 still runs every test.
SDL3_CFLAGS = $(shell pkg-config --cflags sdl3 2>/dev/null)
SDL3_LIBS = $(shell pkg-config --libs sdl3 2>/dev/null)
ifdef RV32_TIMING
SDL3_CFLAGS := $(SDL3_CFLAGS)
SDL3_LIBS := $(SDL3_LIBS)
endif
RV32_RTL := rtl/rv32/rv32_fregfile.v rtl/rv32/rv32_fdecode.v $(FP32_RTL) rtl/rv32/rv32_regfile.v rtl/rv32/rv32_alu.v rtl/rv32/rv32_decode.v rtl/rv32/rv32_muldiv.v rtl/rv32/rv32.v
RV32_SOC_RTL := $(RV32_RTL) rtl/rv32/rv32_bus.v rtl/rv32/rv32_ram.v rtl/rv32/rv32_console.v rtl/rv32/rv32_done.v rtl/rv32/rv32_clint.v rtl/rv32/rv32_plic.v rtl/rv32/rv32_virtio_blk.v rtl/rv32/rv32_bootrom.v rtl/rv32/rv32_input.v rtl/rv32/rv32_display.v rtl/rv32/rv32_dma_window.v rtl/rv32/rv32_soc.v rtl/rv32/rv32_gpu.v rtl/rv32/rv32_g3d.v rtl/rv32/rv32_g3d_core.v rtl/rv32/rv32_simd4.v $(SIMD4_RTL)
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

$(RV32EMU): tools/rv32emu.c tools/rv32_gdb.c tools/rv32_gdb.h $(RV32EMU_CORE) $(RV32_FP_OBJ) | build/rv32
	$(HOST_CC) $(RV32EMU_CFLAGS) -o $@ tools/rv32emu.c tools/rv32_gdb.c tools/rv32emu_core.c tools/rv32_simd4.c tools/rv32_gpu.c tools/rv32_g3d.c $(RV32_FP_OBJ)

build-rv32-emu: toolchain-rv32-emu $(RV32EMU)

toolchain-rv32-win: toolchain-rv32-emu
	@command -v pkg-config >/dev/null || { echo "missing pkg-config (brew install pkg-config)"; exit 1; }
	@pkg-config --exists sdl3 || { echo "missing SDL3 (brew install sdl3)"; exit 1; }
	@echo "SDL3 $$(pkg-config --modversion sdl3)"

# The toolchain check is a prerequisite of the binary, so every target that needs the window says
# what to install rather than failing on a missing header.
$(RV32WIN): tools/rv32win.c $(RV32EMU_CORE) $(RV32_FP_OBJ) | build/rv32 toolchain-rv32-win
	$(HOST_CC) $(RV32EMU_CFLAGS) $(SDL3_CFLAGS) -o $@ tools/rv32win.c tools/rv32emu_core.c tools/rv32_simd4.c tools/rv32_gpu.c tools/rv32_g3d.c $(RV32_FP_OBJ) $(SDL3_LIBS)

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

# The GDB stub (docs/rv32-gdb.md): protocol tests with a built-in client, plus one end-to-end run
# of gdb-multiarch or riscv64-elf-gdb when either is on PATH. debug-rv32-gdb waits for a client:
#   gdb-multiarch build/rv32/selfcheck.elf -ex 'set architecture riscv:rv32' -ex 'target remote :3333'
.PHONY: test-rv32-gdb debug-rv32-gdb
test-rv32-gdb: check-rv32-image $(RV32EMU)
	HOST_CC=$(HOST_CC) RV32_NM=$(RV32_NM) $(PYTHON) -m unittest discover -s tests -p 'test_rv32_gdb.py' -v

debug-rv32-gdb: check-rv32-image $(RV32EMU)
	$(RV32EMU) --image $(or $(IMAGE),build/rv32/selfcheck.bin) --gdb $(or $(PORT),3333)

# Issue #26 (docs/rv32-testing.md): the RV32 checks come in two tiers. test-rv32 is the gate before
# every RV32 PR: every check on Verilator, plus the Icarus runs that take seconds. test-rv32-slow
# holds the long Icarus twins of Verilator checks in test-rv32; run it before merging.
# test-rv32-full is both. All three are safe under make -j.
.PHONY: test-rv32-slow test-rv32-full
test-rv32-full: test-rv32 test-rv32-slow
test-rv32: test-rv32-gdb

$(RV32_TB_VVP): $(RV32_SOC_RTL) $(FP32_HEADERS) $(RV32_TB) | build/rv32
	iverilog -Irtl/fp32 -g2012 -Wall -s rv32_tb -o $@ $(RV32_TB) $(RV32_SOC_RTL)

$(RV32_TB_VERILATOR): $(RV32_SOC_RTL) $(FP32_HEADERS) $(RV32_TB) | build
	verilator -Irtl/fp32 --binary --timing --trace --top-module rv32_tb --Mdir build/verilator-rv32 -o rv32_sim $(RV32_TB) $(RV32_SOC_RTL)

build-rv32-rtl: $(RV32_TB_VVP)

# On Icarus the sweep takes about 300 s in one process, so it is dealt out to RV32_RTL_SHARDS
# processes; each compiles its own testbench, as the unsharded suite does.
RV32_RTL_SHARDS ?= 8
test-rv32-rtl: check-rv32-image
	HOST_CC=$(HOST_CC) RV32_ICARUS_TIMEOUT=$(RV32_ICARUS_TIMEOUT) $(PYTHON) tools/rv32_unittest_shards.py --shards $(RV32_RTL_SHARDS) test_rv32_rtl.py

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
	yosys -Q -T -l build/rv32-soc-synth.log -p 'read_verilog -Irtl/fp32 $(RV32_SOC_RTL); chparam -set RAM_WORDS 64 -set FB_WORDS 64 -set DISK_WORDS 64 rv32_soc; synth -top rv32_soc; check -assert; select -assert-none t:*LATCH*; stat; write_json build/rv32-soc.json'

waves-rv32: $(RV32_TB_VVP) $(RV32EMU)
	$(PYTHON) -m tools.rv32_rtl --mode waves --program loop --emulator $(RV32EMU) --simulator $(RV32_TB_VVP) --rtl-timeout $(RV32_ICARUS_TIMEOUT) --out build/rv32/rtl
	$(PYTHON) -m tools.rv32_rtl --mode waves --program full --emulator $(RV32EMU) --simulator $(RV32_TB_VVP) --rtl-timeout $(RV32_ICARUS_TIMEOUT) --out build/rv32/rtl
	$(PYTHON) -m tools.rv32_rtl --mode waves --program devices --stall 0 --emulator $(RV32EMU) --simulator $(RV32_TB_VVP) --rtl-timeout $(RV32_ICARUS_TIMEOUT) --out build/rv32/rtl
	@echo "Open build/rv32/rtl/loop.vcd, full.vcd, or devices.vcd in Surfer: https://app.surfer-project.org/"

run-rv32-rtl: check-rv32-image $(RV32_TB_VVP) $(RV32EMU)
	$(PYTHON) -m tools.rv32_rtl --image build/rv32/selfcheck.bin --expect-console "PASS $(RV32_SELFCHECK_HEX)" --emulator $(RV32EMU) --simulator $(RV32_TB_VVP) --rtl-timeout $(RV32_ICARUS_TIMEOUT) --out build/rv32/rtl

# Each runner target has its own output directory, so `make -j` cannot interleave two runs' traces.
run-rv32-rtl-verilator: check-rv32-image $(RV32_TB_VERILATOR) $(RV32EMU)
	$(PYTHON) -m tools.rv32_rtl --image build/rv32/selfcheck.bin --expect-console "PASS $(RV32_SELFCHECK_HEX)" --emulator $(RV32EMU) --simulator $(RV32_TB_VERILATOR) --out build/rv32/rtl-verilator --stall 1

bench-rv32-rtl: $(RV32_TB_VVP) $(RV32EMU)
	$(PYTHON) -m tools.rv32_rtl --mode bench --emulator $(RV32EMU) --simulator $(RV32_TB_VVP) --rtl-timeout $(RV32_ICARUS_TIMEOUT) --out build/rv32/rtl

# The device diagnostic reads the timer, so the two backends are compared at the results level
# (console, outcome, checkpoints, and the trap records in order), not trace for trace
# (docs/rv32.md, "Device time").
run-rv32-diag-emu: check-rv32-image $(RV32EMU)
	$(PYTHON) -m tools.rv32_rtl $(RV32_DIAG_ARGS) --backend emulator --frames build/rv32/frames --emulator $(RV32EMU) --out build/rv32/emu

# llvm@22's diag runs 406k steps (2.1M cycles at stall 1); clang 18's runs 3.8M (about 19M cycles),
# past the testbench's 10M default, so the budget is explicit and leaves room for either.
run-rv32-diag-rtl: check-rv32-image $(RV32_TB_VVP) $(RV32EMU)
	$(PYTHON) -m tools.rv32_rtl $(RV32_DIAG_ARGS) --emulator $(RV32EMU) --simulator $(RV32_TB_VVP) --rtl-timeout $(RV32_ICARUS_TIMEOUT) --max-cycles 40000000 --out build/rv32/rtl

run-rv32-diag-rtl-verilator: check-rv32-image $(RV32_TB_VERILATOR) $(RV32EMU)
	$(PYTHON) -m tools.rv32_rtl $(RV32_DIAG_ARGS) --emulator $(RV32EMU) --simulator $(RV32_TB_VERILATOR) --stall 1 --max-cycles 40000000 --out build/rv32/rtl-verilator

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
	$(PYTHON) -m tools.rv32_rtl $(RV32_PONG_ARGS) --emulator $(RV32EMU) --simulator $(RV32_TB_VVP) --rtl-timeout $(RV32_ICARUS_TIMEOUT) --out build/rv32/rtl

run-rv32-pong-rtl-verilator: check-rv32-image $(RV32_TB_VERILATOR) $(RV32EMU)
	$(PYTHON) -m tools.rv32_rtl $(RV32_PONG_ARGS) --emulator $(RV32EMU) --simulator $(RV32_TB_VERILATOR) --stall 1 --out build/rv32/rtl-verilator

test-rv32: test-rv32-tools test-rv32-rt test-rv32-pong run-rv32-qemu test-rv32-emu test-rv32-win run-rv32-emu diff-rv32-qemu run-rv32-diag-emu run-rv32-pong-emu test-rv32-rtl-verilator run-rv32-rtl run-rv32-rtl-verilator run-rv32-diag-rtl-verilator run-rv32-pong-rtl-verilator lint-rv32 lint-rv32-soc synth-rv32 synth-rv32-soc
test-rv32-slow: test-rv32-rtl run-rv32-diag-rtl run-rv32-pong-rtl

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
test-rv32-capstone: $(RV32_DIGIT_GENERATED) $(RV32_G3D_GENERATED)
	HOST_CC=$(HOST_CC) $(PYTHON) -m unittest discover -s tests -p 'test_rv32_capstone.py' -v

run-rv32-capstone: check-rv32-image $(RV32WIN)
	@echo "UP/DOWN select, ENTER plays, ESC returns, Q quits. Both games: P pauses, R restarts."
	$(RV32WIN) --image build/rv32/capstone.bin --scale 3 --record build/rv32/capstone.recorded.input --checkpoints build/rv32/capstone.recorded.checkpoints

run-rv32-capstone-emu: check-rv32-image $(RV32EMU)
	$(PYTHON) -m tools.rv32_rtl $(RV32_CAPSTONE_ARGS) --backend emulator --emulator $(RV32EMU) --out build/rv32/emu

run-rv32-capstone-rtl: check-rv32-image $(RV32_TB_VVP) $(RV32EMU)
	$(PYTHON) -m tools.rv32_rtl $(RV32_CAPSTONE_ARGS) --max-cycles 20000000 --emulator $(RV32EMU) --simulator $(RV32_TB_VVP) --rtl-timeout $(RV32_ICARUS_TIMEOUT) --out build/rv32/rtl

run-rv32-capstone-rtl-verilator: check-rv32-image $(RV32_TB_VERILATOR) $(RV32EMU)
	$(PYTHON) -m tools.rv32_rtl $(RV32_CAPSTONE_ARGS) --max-cycles 20000000 --emulator $(RV32EMU) --simulator $(RV32_TB_VERILATOR) --stall 1 --out build/rv32/rtl-verilator

frames-rv32-capstone: check-rv32-image $(RV32EMU)
	$(PYTHON) -m tools.rv32_rtl $(RV32_CAPSTONE_ARGS) --backend emulator --frames build/rv32/capstone-frames --emulator $(RV32EMU) --out build/rv32/emu

disasm-rv32-capstone: firmware-rv32
	cat build/rv32/capstone.lst

test-rv32: test-rv32-capstone run-rv32-capstone-emu run-rv32-capstone-rtl-verilator
test-rv32-slow: run-rv32-capstone-rtl

# Compile the same directed C checks as a standalone sanitized executable.
.PHONY: test-rv32-capstone-sanitize
test-rv32-capstone-sanitize: build/rv32/digit_shape.h build/rv32/digit_weights.h build/rv32/digit_weights.c $(RV32_G3D_GENERATED) | build/rv32
	mkdir -p build/rv32/host
	$(HOST_CC) -std=c11 -O1 -g -Wall -Wextra -Werror -fno-builtin -fsanitize=address,undefined -fno-sanitize-recover=undefined -fno-omit-frame-pointer -DRV32_NATIVE_MAIN -Iprograms/rv32 -Ibuild/rv32 tests/rv32_capstone_native.c programs/rv32/gpu_demo.c programs/rv32/gpu_ref.c programs/rv32/runtime.c programs/rv32/tetris_game.c programs/rv32/pong_game.c programs/rv32/gfx.c programs/rv32/gfx_text.c programs/rv32/digit_ui.c programs/rv32/digit_model.c build/rv32/digit_weights.c programs/rv32/g3d_demo.c programs/rv32/g3d_ref.c -o build/rv32/host/capstone-sanitize
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

test-rv32: test-rv32-f-verilator test-rv32-f-tools run-rv32-f-emu run-rv32-f-rtl run-rv32-f-rtl-verilator
test-rv32-slow: test-rv32-f

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
	$(PYTHON) tools/rv32_rtl.py $(RV32_SIMD4_ARGS) --simulator $(RV32_TB_VVP) --rtl-timeout $(RV32_ICARUS_TIMEOUT) --out build/rv32/simd4-icarus

run-rv32-simd4-rtl-verilator: check-rv32-simd4-image $(RV32EMU) $(RV32_TB_VERILATOR)
	$(PYTHON) tools/rv32_rtl.py $(RV32_SIMD4_ARGS) --simulator $(RV32_TB_VERILATOR) --out build/rv32/simd4-verilator

waves-rv32-simd4: check-rv32-simd4-image $(RV32EMU) $(RV32_TB_VVP)
	$(PYTHON) tools/rv32_rtl.py $(RV32_SIMD4_ARGS) --simulator $(RV32_TB_VVP) --rtl-timeout $(RV32_ICARUS_TIMEOUT) --mode waves --out build/rv32/simd4-waves

build/rv32/simd4-protocol.vvp: rtl/rv32/rv32_simd4.v $(SIMD4_RTL) tests/rv32_simd4_tb.sv | build/rv32
	iverilog -g2012 -Wall -s rv32_simd4_tb -o $@ tests/rv32_simd4_tb.sv rtl/rv32/rv32_simd4.v $(SIMD4_RTL)

build/verilator-rv32-simd4/protocol: rtl/rv32/rv32_simd4.v $(SIMD4_RTL) tests/rv32_simd4_tb.sv | build
	verilator --binary --timing --trace --top-module rv32_simd4_tb --Mdir build/verilator-rv32-simd4 -o protocol tests/rv32_simd4_tb.sv rtl/rv32/rv32_simd4.v $(SIMD4_RTL)

.PHONY: test-rv32-simd4 test-rv32-simd4-verilator
test-rv32-simd4: check-rv32-simd4-image $(RV32EMU) $(RV32_TB_VVP) build/rv32/simd4-protocol.vvp
	$(PYTHON) -m unittest discover -s tests -p 'test_rv32_simd4.py' -v

test-rv32-simd4-verilator: check-rv32-simd4-image $(RV32EMU) $(RV32_TB_VERILATOR) build/verilator-rv32-simd4/protocol
	A2_SIM=verilator $(PYTHON) -m unittest discover -s tests -p 'test_rv32_simd4.py' -v

test-rv32: test-rv32-simd4-verilator run-rv32-simd4-emu run-rv32-simd4-rtl run-rv32-simd4-rtl-verilator
test-rv32-slow: test-rv32-simd4

# G1: integer rasterizer, RAM/framebuffer blits, and menu integration.
.PHONY: test-rv32-gfx test-rv32-gfx-verilator check-rv32-gfx-image run-rv32-gfx-emu run-rv32-gfx-rtl run-rv32-gfx-rtl-verilator run-rv32-gfx-menu-emu run-rv32-gfx-menu-rtl run-rv32-gfx-menu-rtl-verilator lint-rv32-gfx synth-rv32-gfx
RV32_GFX_MAX_CYCLES := 150000000
RV32_GFX_MENU_HEX := 8ed0d4a0
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
# Issue #20: the DMA window bounds G1's blit source and G2's depth buffer (tests/test_rv32_dma_window.py).
.PHONY: test-rv32-dma-window test-rv32-dma-window-icarus
test-rv32-dma-window: $(RV32EMU) $(RV32_TB_VERILATOR)
	G1_SIM=verilator $(PYTHON) -m unittest discover -s tests -p 'test_rv32_dma_window.py' -v
test-rv32-dma-window-icarus: $(RV32EMU) $(RV32_TB_VVP)
	$(PYTHON) -m unittest discover -s tests -p 'test_rv32_dma_window.py' -v
test-rv32: test-rv32-dma-window test-rv32-dma-window-icarus
run-rv32-gfx-emu: check-rv32-gfx-image $(RV32EMU)
	$(PYTHON) tools/rv32_rtl.py $(RV32_GFX_ARGS) --backend emulator --out build/gfx/emu
run-rv32-gfx-rtl: check-rv32-gfx-image $(RV32EMU) $(RV32_TB_VVP)
	$(PYTHON) tools/rv32_rtl.py $(RV32_GFX_ARGS) --max-cycles $(RV32_GFX_MAX_CYCLES) --simulator $(RV32_TB_VVP) --rtl-timeout $(RV32_ICARUS_TIMEOUT) --out build/gfx/icarus
run-rv32-gfx-rtl-verilator: check-rv32-gfx-image $(RV32EMU) $(RV32_TB_VERILATOR)
	$(PYTHON) tools/rv32_rtl.py $(RV32_GFX_ARGS) --max-cycles $(RV32_GFX_MAX_CYCLES) --simulator $(RV32_TB_VERILATOR) --stall 1 --gpu-stall 2 --out build/gfx/verilator
run-rv32-gfx-menu-emu: check-rv32-image $(RV32EMU)
	$(PYTHON) tools/rv32_rtl.py $(RV32_GFX_MENU_ARGS) --backend emulator --out build/gfx/menu-emu
run-rv32-gfx-menu-rtl: check-rv32-image $(RV32EMU) $(RV32_TB_VVP)
	$(PYTHON) tools/rv32_rtl.py $(RV32_GFX_MENU_ARGS) --max-cycles $(RV32_GFX_MAX_CYCLES) --simulator $(RV32_TB_VVP) --rtl-timeout $(RV32_ICARUS_TIMEOUT) --out build/gfx/menu-icarus
run-rv32-gfx-menu-rtl-verilator: check-rv32-image $(RV32EMU) $(RV32_TB_VERILATOR)
	$(PYTHON) tools/rv32_rtl.py $(RV32_GFX_MENU_ARGS) --max-cycles $(RV32_GFX_MAX_CYCLES) --simulator $(RV32_TB_VERILATOR) --seed 17 --gpu-seed 31 --out build/gfx/menu-verilator
lint-rv32-gfx:
	verilator --lint-only --Wall --language 1364-2005 --top-module rv32_gpu rtl/rv32/rv32_gpu.v
synth-rv32-gfx: | build
	yosys -Q -T -l build/gpu-synth.log -p 'read_verilog rtl/rv32/rv32_gpu.v; synth -top rv32_gpu; check -assert; select -assert-none t:*LATCH*; stat; write_json build/gpu.json'
test-rv32: test-rv32-gfx test-rv32-gfx-verilator run-rv32-gfx-emu run-rv32-gfx-rtl-verilator run-rv32-gfx-menu-emu run-rv32-gfx-menu-rtl-verilator lint-rv32-gfx synth-rv32-gfx
test-rv32-slow: run-rv32-gfx-rtl

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
	build/gfx/sanitize build/gfx/unit-icarus/commands.txt
test-rv32: test-rv32-gfx-sanitize
# The capstone sanitizer covers the runtime, the digit UI and the digit model
# under ASan and UBSan. It was defined but never reached by the aggregate.
test-rv32: test-rv32-capstone-sanitize

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
# The tables are defined once here. As `static const` in the header they cost a
# private copy in every translation unit that included it.
build/rv32/digit_weights.c: build/rv32/digit_weights.h $(RV32_DIGIT_MODEL_DEPS) | build/rv32
	$(PYTHON) -m tools.rv32_digit_model weights-source $@
build/rv32/digit_weights.o: build/rv32/digit_weights.c build/rv32/digit_weights.h | build/rv32
	$(RV32_CC) $(RV32_CFLAGS) -Ibuild/rv32 -c $< -o $@
build/rv32/digit_check.h: $(RV32_DIGIT_MODEL_DEPS) $(RV32_MNIST) | build/rv32
	$(PYTHON) -m tools.rv32_digit_model check $@
# Every object that reaches digit_model.h needs the generated shape header; only
# the files that actually multiply also need the weights.
build/rv32/digit_ui.o: programs/rv32/digit_ui.c build/rv32/digit_shape.h $(RV32_HEADERS) | build/rv32
	$(RV32_CC) $(RV32_CFLAGS) -Ibuild/rv32 -c $< -o $@
build/rv32/runtime.o: programs/rv32/runtime.c build/rv32/digit_shape.h $(RV32_HEADERS) | build/rv32
	$(RV32_CC) $(RV32_CFLAGS) -Ibuild/rv32 -c $< -o $@
build/rv32/g3d_demo.o: programs/rv32/g3d_demo.c $(RV32_G3D_GENERATED) $(RV32_HEADERS) | build/rv32
	$(RV32_CC) $(RV32_CFLAGS) -Ibuild/rv32 -c $< -o $@
build/rv32/capstone.o: programs/rv32/capstone.c $(RV32_DIGIT_GENERATED) $(RV32_HEADERS) | build/rv32
	$(RV32_CC) $(RV32_CFLAGS) -Ibuild/rv32 -c $< -o $@
build/rv32/digit_model.o: programs/rv32/digit_model.c build/rv32/digit_weights.h $(RV32_HEADERS) | build/rv32
	$(RV32_CC) $(RV32_CFLAGS) -Ibuild/rv32 -c $< -o $@
build/rv32/digit_hw.o: programs/rv32/digit_hw.c build/rv32/digit_weights.h build/rv32/digit_kernels.h $(RV32_HEADERS) | build/rv32
	$(RV32_CC) $(RV32_CFLAGS) -Ibuild/rv32 -c $< -o $@
build/rv32/digitcheck.o: programs/rv32/digitcheck.c $(RV32_DIGIT_GENERATED) $(RV32_HEADERS) | build/rv32
	$(RV32_CC) $(RV32_CFLAGS) -Ibuild/rv32 -c $< -o $@
build/rv32/digitcheck.elf: build/rv32/digitcheck.o build/rv32/digit_model.o build/rv32/digit_hw.o build/rv32/digit_weights.o build/rv32/simd4.o $(RV32_COMMON_OBJS) programs/rv32/link.ld
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
# Both acceptance measurements, each failing below its floor: clean MNIST (95%),
# then the pinned keyboard-style set (85% at every height; MNIST test digits
# redrawn one brush wide at nominal heights 10 to 28 pixels).
accuracy-rv32-digit:
	$(PYTHON) -m tools.digit_ref --count 10000
	$(PYTHON) -m tools.digit_drawn_accuracy
run-rv32-digit-emu: check-rv32-digit-image $(RV32EMU)
	$(PYTHON) tools/rv32_rtl.py $(RV32_DIGIT_ARGS) --backend emulator --out build/digit/emu
run-rv32-digit-rtl: check-rv32-digit-image $(RV32EMU) $(RV32_TB_VVP)
	$(PYTHON) tools/rv32_rtl.py $(RV32_DIGIT_ARGS) --max-cycles $(RV32_DIGIT_MAX_CYCLES) --simulator $(RV32_TB_VVP) --rtl-timeout $(RV32_ICARUS_TIMEOUT) --out build/digit/icarus
run-rv32-digit-rtl-verilator: check-rv32-digit-image $(RV32EMU) $(RV32_TB_VERILATOR)
	$(PYTHON) tools/rv32_rtl.py $(RV32_DIGIT_ARGS) --max-cycles $(RV32_DIGIT_MAX_CYCLES) --simulator $(RV32_TB_VERILATOR) --stall 1 --simd-stall 2 --out build/digit/verilator

# The menu session: the guest draws two digits with the keyboard and classifies
# them. Results mode, because the classification touches accelerator registers.
# The Icarus variant is available separately, as the G1 menu replay is.
.PHONY: run-rv32-digit-menu-emu run-rv32-digit-menu-rtl run-rv32-digit-menu-rtl-verilator
RV32_DIGIT_MENU_HEX := b20bf8ad
RV32_DIGIT_MENU_ARGS = --image build/rv32/capstone.bin --input programs/rv32/digit.input \
                       --expect-checkpoints programs/rv32/digit.expected \
                       --expect-last-line "PASS $(RV32_DIGIT_MENU_HEX)" --compare results \
                       --emulator $(RV32EMU) --timeout 900
run-rv32-digit-menu-emu: check-rv32-image $(RV32EMU)
	$(PYTHON) tools/rv32_rtl.py $(RV32_DIGIT_MENU_ARGS) --backend emulator --out build/digit/menu-emu
run-rv32-digit-menu-rtl: check-rv32-image $(RV32EMU) $(RV32_TB_VVP)
	$(PYTHON) tools/rv32_rtl.py $(RV32_DIGIT_MENU_ARGS) --max-cycles $(RV32_DIGIT_MAX_CYCLES) --simulator $(RV32_TB_VVP) --rtl-timeout $(RV32_ICARUS_TIMEOUT) --out build/digit/menu-icarus
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
build/rv32/digitbench_%.elf: build/rv32/digitbench_%.o build/rv32/digit_model.o build/rv32/digit_hw.o build/rv32/digit_weights.o build/rv32/simd4.o $(RV32_COMMON_OBJS) programs/rv32/link.ld
	$(RV32_CC) $(RV32_LDFLAGS) -o $@ $(filter %.o,$^)
RV32_DIGIT_BENCH_BINS := $(foreach v,cpu hw,$(foreach n,1 5,build/rv32/digitbench_$(v)_$(n).bin))
bench-rv32-digit: $(RV32_DIGIT_BENCH_BINS) $(RV32EMU) $(RV32_TB_VERILATOR)
	$(PYTHON) tools/rv32_digit_bench.py --emulator $(RV32EMU) --simulator $(RV32_TB_VERILATOR)
# The single-inference bench image, not the diagnostic: dumping all eight
# classifications and both recovery paths produced a five-gigabyte VCD, and one
# inference already contains every launch edge worth looking at.
# The expected line is the bench sink for the committed model: 1 + the predicted
# class + its margin for canvas 0. Retraining changes it.
waves-rv32-digit: build/rv32/digitbench_hw_1.bin $(RV32EMU) $(RV32_TB_VVP)
	$(PYTHON) tools/rv32_rtl.py --image build/rv32/digitbench_hw_1.bin --compare results \
	  --expect-last-line "bench 00001b54" --emulator $(RV32EMU) --timeout 900 \
	  --max-cycles $(RV32_DIGIT_MAX_CYCLES) --simulator $(RV32_TB_VVP) --rtl-timeout $(RV32_ICARUS_TIMEOUT) --mode waves --out build/digit/waves

# G2: programmable 3D. The Python oracle, the guest C reference, the emulator
# device and the RTL are compared bit for bit; g3dcheck checks the device from
# the guest against counters and image hashes the oracle generated.
.PHONY: test-rv32-3d check-rv32-3d-image run-rv32-3d-emu
RV32_G3D_ARGS = --image build/rv32/g3dcheck.bin --compare results --expect-last-line "PASS G2" --emulator $(RV32EMU) --timeout 900
# One generator run writes both headers. Make 3.81 has no grouped targets, so the
# scenes header is the real target and the shaders header follows it: two parallel
# generator runs could otherwise race on the same files.
build/rv32/g3d_scenes.h: $(RV32_G3D_DEPS) | build/rv32
	$(PYTHON) tools/rv32_g3d_header.py --out build/rv32
build/rv32/g3d_shaders.h: build/rv32/g3d_scenes.h
	@test -f $@ || $(PYTHON) tools/rv32_g3d_header.py --out build/rv32
build/rv32/g3dcheck.o: programs/rv32/g3dcheck.c $(RV32_G3D_GENERATED) $(RV32_HEADERS) | build/rv32
	$(RV32_CC) $(RV32_CFLAGS) -Ibuild/rv32 -c $< -o $@
build/rv32/g3dcheck.elf: build/rv32/g3dcheck.o build/rv32/g3d.o $(RV32_COMMON_OBJS) programs/rv32/link.ld
	$(RV32_CC) $(RV32_LDFLAGS) -Wl,-Map,$(@:.elf=.map) -o $@ $(filter %.o,$^)
check-rv32-3d-image: build/rv32/g3dcheck.bin build/rv32/g3dcheck.lst
	$(PYTHON) tools/rv32_image.py build/rv32/g3dcheck.elf --listing build/rv32/g3dcheck.lst --bin build/rv32/g3dcheck.bin --hex build/rv32/g3dcheck.hex
RV32_G3D_MAX_CYCLES := 200000000
test-rv32-3d: $(RV32EMU) $(RV32_TB_VVP) | build
	HOST_CC=$(HOST_CC) $(PYTHON) -m unittest discover -s tests -p 'test_rv32_3d*.py' -v
# test-rv32-3d in two halves for the tiers (issue #26): the files that run no simulator (the
# oracle, the C reference, the emulator's device) in test-rv32, and the Icarus corpus and SoC
# contracts in test-rv32-slow. Together they run what test-rv32-3d runs, without two processes
# rebuilding the same libraries in build/g3d at once.
.PHONY: test-rv32-3d-model test-rv32-3d-icarus
test-rv32-3d-model: $(RV32EMU) | build
	HOST_CC=$(HOST_CC) $(PYTHON) -m unittest tests.test_rv32_3d tests.test_rv32_3d_device -v
test-rv32-3d-icarus: $(RV32EMU) $(RV32_TB_VVP) | build
	HOST_CC=$(HOST_CC) $(PYTHON) -m unittest tests.test_rv32_3d_rtl tests.test_rv32_3d_soc -v
# Runs the standalone corpus in full and the SoC contracts on Verilator; it fails
# when Verilator is missing because both tests build with it.
test-rv32-3d-verilator: $(RV32EMU) $(RV32_TB_VERILATOR) | build
	G2_SIM=verilator $(PYTHON) -m unittest tests.test_rv32_3d_rtl tests.test_rv32_3d_soc -v
run-rv32-3d-emu: check-rv32-3d-image $(RV32EMU)
	$(PYTHON) tools/rv32_rtl.py $(RV32_G3D_ARGS) --backend emulator --out build/g3d/emu
run-rv32-3d-rtl: check-rv32-3d-image $(RV32EMU) $(RV32_TB_VVP)
	$(PYTHON) tools/rv32_rtl.py $(RV32_G3D_ARGS) --max-cycles $(RV32_G3D_MAX_CYCLES) --simulator $(RV32_TB_VVP) --rtl-timeout $(RV32_ICARUS_TIMEOUT) --out build/g3d/icarus
run-rv32-3d-rtl-verilator: check-rv32-3d-image $(RV32EMU) $(RV32_TB_VERILATOR)
	$(PYTHON) tools/rv32_rtl.py $(RV32_G3D_ARGS) --max-cycles $(RV32_G3D_MAX_CYCLES) --simulator $(RV32_TB_VERILATOR) --stall 1 --gpu-stall 2 --out build/g3d/verilator
.PHONY: test-rv32-3d-verilator run-rv32-3d-rtl run-rv32-3d-rtl-verilator lint-rv32-3d synth-rv32-3d
lint-rv32-3d:
	verilator --lint-only --Wall --language 1364-2005 --top-module rv32_g3d rtl/rv32/rv32_g3d.v rtl/rv32/rv32_g3d_core.v
synth-rv32-3d: | build
	yosys -Q -T -l build/g3d-synth.log -p 'read_verilog rtl/rv32/rv32_g3d.v rtl/rv32/rv32_g3d_core.v; synth -top rv32_g3d; check -assert; select -assert-none t:*LATCH*; stat; write_json build/g3d.json'
test-rv32: test-rv32-3d-model test-rv32-3d-verilator run-rv32-3d-emu run-rv32-3d-rtl-verilator lint-rv32-3d synth-rv32-3d
test-rv32-slow: test-rv32-3d-icarus
# The 3D menu replay: the device draws every 3D frame on the emulator and the RTL,
# the C reference draws them natively, and all three must match the pinned
# checkpoints. Re-pin with tools/rv32_capstone_native.py --input programs/rv32/g3d.input --write.
.PHONY: run-rv32-3d-menu-emu run-rv32-3d-menu-rtl run-rv32-3d-menu-rtl-verilator
RV32_3D_MENU_HEX := 278a4eac
RV32_3D_MENU_ARGS = --image build/rv32/capstone.bin --input programs/rv32/g3d.input --expect-checkpoints programs/rv32/g3d.expected --expect-last-line "PASS $(RV32_3D_MENU_HEX)" --compare results --emulator $(RV32EMU) --timeout 1800
run-rv32-3d-menu-emu: check-rv32-image $(RV32EMU)
	$(PYTHON) tools/rv32_rtl.py $(RV32_3D_MENU_ARGS) --backend emulator --out build/g3d/menu-emu
run-rv32-3d-menu-rtl: check-rv32-image $(RV32EMU) $(RV32_TB_VVP)
	$(PYTHON) tools/rv32_rtl.py $(RV32_3D_MENU_ARGS) --max-cycles $(RV32_G3D_MAX_CYCLES) --simulator $(RV32_TB_VVP) --rtl-timeout $(RV32_ICARUS_TIMEOUT) --out build/g3d/menu-icarus
run-rv32-3d-menu-rtl-verilator: check-rv32-image $(RV32EMU) $(RV32_TB_VERILATOR)
	$(PYTHON) tools/rv32_rtl.py $(RV32_3D_MENU_ARGS) --max-cycles $(RV32_G3D_MAX_CYCLES) --simulator $(RV32_TB_VERILATOR) --seed 17 --gpu-seed 31 --out build/g3d/menu-verilator
test-rv32: run-rv32-3d-menu-emu run-rv32-3d-menu-rtl-verilator

# One demo frame drawn by the C reference on the RV32I CPU and by the device.
.PHONY: bench-rv32-3d
build/rv32/g3dbench_cpu_%.o: programs/rv32/g3dbench.c $(RV32_G3D_GENERATED) $(RV32_HEADERS) | build/rv32
	$(RV32_CC) $(RV32_CFLAGS) -Ibuild/rv32 -DG3D_BENCH_CPU -DG3D_BENCH_COUNT=$*u -c $< -o $@
build/rv32/g3dbench_hw_%.o: programs/rv32/g3dbench.c $(RV32_G3D_GENERATED) $(RV32_HEADERS) | build/rv32
	$(RV32_CC) $(RV32_CFLAGS) -Ibuild/rv32 -DG3D_BENCH_COUNT=$*u -c $< -o $@
build/rv32/g3dbench_%.elf: build/rv32/g3dbench_%.o build/rv32/g3d_demo.o build/rv32/g3d_ref.o build/rv32/g3d.o build/rv32/gpu.o build/rv32/gpu_ref.o build/rv32/gfx.o build/rv32/gfx_text.o $(RV32_COMMON_OBJS) programs/rv32/link.ld
	$(RV32_CC) $(RV32_LDFLAGS) -o $@ $(filter %.o,$^)
RV32_G3D_BENCH_BINS := $(foreach v,cpu hw,$(foreach n,1 3,build/rv32/g3dbench_$(v)_$(n).bin))
bench-rv32-3d: $(RV32_G3D_BENCH_BINS) $(RV32EMU) $(RV32_TB_VERILATOR)
	$(PYTHON) tools/rv32_g3d_bench.py --emulator $(RV32EMU) --simulator $(RV32_TB_VERILATOR)
# One small job, not a frame: the dump stays around a megabyte and the phase
# totals are checked against the oracle's cycle count.
.PHONY: waves-rv32-3d
waves-rv32-3d: | build
	$(PYTHON) tools/rv32_g3d_waves.py
.PHONY: test-rv32-3d-sanitize
test-rv32-3d-sanitize: $(RV32_G3D_DEPS) tools/rv32_g3d_corpus.py tools/rv32_g3d_scene.py | build
	mkdir -p build/g3d
	$(PYTHON) tools/rv32_g3d_corpus.py > build/g3d/corpus.txt
	$(HOST_CC) -std=c11 -O1 -g -Wall -Wextra -Werror -fsanitize=address,undefined -fno-sanitize-recover=undefined -fno-omit-frame-pointer -DG3D_NATIVE_MAIN -Iprograms/rv32 tests/rv32_g3d_native.c tools/rv32_g3d.c programs/rv32/g3d_ref.c -o build/g3d/sanitize
	build/g3d/sanitize build/g3d/corpus.txt
test-rv32: test-rv32-3d-sanitize

# S1: the three accelerators in one image. soccheck overlaps each pair the
# contract allows (SIMD4 with G1, SIMD4 with G2), proves the pair it forbids
# (G1 with G2) faults without side effects, and checks that a reset or a fault in
# one engine leaves the others' exact results alone. It installs a trap handler,
# so its image may use the CSR instructions and mret. Results mode, and no
# --compare-stores: the counters it reads include device time.
.PHONY: check-rv32-soc-image run-rv32-soc-emu run-rv32-soc-rtl run-rv32-soc-rtl-verilator
RV32_SOC_ARGS = --image build/rv32/soccheck.bin --compare results --expect-last-line "PASS S1" --emulator $(RV32EMU) --timeout 3600
RV32_SOC_MAX_CYCLES := 200000000
build/rv32/soccheck.o: programs/rv32/soccheck.c $(RV32_DIGIT_GENERATED) $(RV32_G3D_GENERATED) $(RV32_HEADERS) | build/rv32
	$(RV32_CC) $(RV32_CFLAGS) -Ibuild/rv32 -c $< -o $@
build/rv32/soccheck.elf: build/rv32/soccheck.o build/rv32/trap.o build/rv32/gpu.o build/rv32/gpu_ref.o build/rv32/gfx.o build/rv32/g3d.o build/rv32/simd4.o build/rv32/digit_model.o build/rv32/digit_hw.o build/rv32/digit_weights.o $(RV32_COMMON_OBJS) programs/rv32/link.ld
	$(RV32_CC) $(RV32_LDFLAGS) -Wl,-Map,$(@:.elf=.map) -o $@ $(filter %.o,$^)
check-rv32-soc-image: build/rv32/soccheck.bin build/rv32/soccheck.lst
	$(PYTHON) tools/rv32_image.py build/rv32/soccheck.elf --listing build/rv32/soccheck.lst --bin build/rv32/soccheck.bin --hex build/rv32/soccheck.hex --allow-privileged
run-rv32-soc-emu: check-rv32-soc-image $(RV32EMU)
	$(PYTHON) tools/rv32_rtl.py $(RV32_SOC_ARGS) --backend emulator --out build/soc/emu
run-rv32-soc-rtl: check-rv32-soc-image $(RV32EMU) $(RV32_TB_VVP)
	$(PYTHON) tools/rv32_rtl.py $(RV32_SOC_ARGS) --max-cycles $(RV32_SOC_MAX_CYCLES) --simulator $(RV32_TB_VVP) --rtl-timeout $(RV32_ICARUS_TIMEOUT) --out build/soc/icarus
# Both stall groups at once: CPU memory, the shared engine port and the SIMD4 buffers.
run-rv32-soc-rtl-verilator: check-rv32-soc-image $(RV32EMU) $(RV32_TB_VERILATOR)
	$(PYTHON) tools/rv32_rtl.py $(RV32_SOC_ARGS) --max-cycles $(RV32_SOC_MAX_CYCLES) --simulator $(RV32_TB_VERILATOR) --stall 1 --gpu-stall 2 --simd-stall 2 --out build/soc/verilator
test-rv32: run-rv32-soc-emu run-rv32-soc-rtl-verilator
# The S1 menu session: 2D, 3D, digit, then 2D again, in one boot. Seeded waits on
# CPU memory, the shared engine port and the SIMD4 buffers at once. Re-pin with
# tools/rv32_capstone_native.py --input programs/rv32/soc.input --write.
.PHONY: run-rv32-soc-menu-emu run-rv32-soc-menu-rtl run-rv32-soc-menu-rtl-verilator
RV32_SOC_MENU_HEX := c76cd363
RV32_SOC_MENU_ARGS = --image build/rv32/capstone.bin --input programs/rv32/soc.input --expect-checkpoints programs/rv32/soc.expected --expect-last-line "PASS $(RV32_SOC_MENU_HEX)" --compare results --emulator $(RV32EMU) --timeout 1800
run-rv32-soc-menu-emu: check-rv32-image $(RV32EMU)
	$(PYTHON) tools/rv32_rtl.py $(RV32_SOC_MENU_ARGS) --backend emulator --out build/soc/menu-emu
run-rv32-soc-menu-rtl: check-rv32-image $(RV32EMU) $(RV32_TB_VVP)
	$(PYTHON) tools/rv32_rtl.py $(RV32_SOC_MENU_ARGS) --max-cycles $(RV32_SOC_MAX_CYCLES) --simulator $(RV32_TB_VVP) --rtl-timeout $(RV32_ICARUS_TIMEOUT) --out build/soc/menu-icarus
run-rv32-soc-menu-rtl-verilator: check-rv32-image $(RV32EMU) $(RV32_TB_VERILATOR)
	$(PYTHON) tools/rv32_rtl.py $(RV32_SOC_MENU_ARGS) --max-cycles $(RV32_SOC_MAX_CYCLES) --simulator $(RV32_TB_VERILATOR) --seed 17 --gpu-seed 31 --simd-seed 43 --out build/soc/menu-verilator
test-rv32: run-rv32-soc-menu-emu run-rv32-soc-menu-rtl-verilator

# Track 0: the M extension. The same sources built for RV32IM: the compiler emits
# mul/div/rem, and every image except the self-check (which calls the software
# routines by name to test them) links without programs/rv32/rt/muldiv.c. Each
# image must reproduce the RV32I build's results exactly: the same console line,
# the same checkpoints, on the emulator and on both simulators.
.PHONY: firmware-rv32m check-rv32m-image run-rv32m-emu run-rv32m-rtl run-rv32m-rtl-verilator test-rv32-m test-rv32-m-verilator
RV32M_CFLAGS := $(subst -march=rv32i ,-march=rv32im ,$(RV32_CFLAGS)) -Ibuild/rv32
RV32M_LDFLAGS := $(subst -march=rv32i ,-march=rv32im ,$(RV32_LDFLAGS))
# The substitution needs -march=rv32i as a whole word in RV32_ARCH. If it misses, the flags
# stay RV32I and the self-check (checked with --allow-m only) would pass as an RV32I build,
# so every RV32IM link stops with this error instead.
RV32M_MARCH_CHECK = $(if $(and $(filter -march=rv32im,$(RV32M_CFLAGS)),$(filter -march=rv32im,$(RV32M_LDFLAGS))),,\
	$(error RV32M_CFLAGS/RV32M_LDFLAGS did not get -march=rv32im; check -march=rv32i in RV32_ARCH))
RV32M_IMAGES := selfcheck diag pong capstone
RV32M_COMMON_OBJS := build/rv32m/start.o build/rv32m/console.o
# Each image's RV32I objects in build/rv32m, without the common ones (muldiv.o among them).
rv32m_objs = $(patsubst build/rv32/%,build/rv32m/%,$(filter-out $(RV32_COMMON_OBJS),$(1)))
RV32M_OBJS_selfcheck := $(call rv32m_objs,$(RV32_SELFCHECK_OBJS)) build/rv32m/muldiv.o
RV32M_OBJS_diag := $(call rv32m_objs,$(RV32_DIAG_OBJS))
RV32M_OBJS_pong := $(call rv32m_objs,$(RV32_PONG_OBJS))
RV32M_OBJS_capstone := $(call rv32m_objs,$(RV32_CAPSTONE_OBJS))
build/rv32m:
	mkdir -p $@
build/rv32m/%.o: programs/rv32/%.c $(RV32_HEADERS) $(RV32_DIGIT_GENERATED) $(RV32_G3D_GENERATED) | build/rv32m
	$(RV32_CC) $(RV32M_CFLAGS) -c -o $@ $<
build/rv32m/%.o: programs/rv32/%.S programs/rv32/board.h | build/rv32m
	$(RV32_CC) $(RV32M_CFLAGS) -c -o $@ $<
build/rv32m/muldiv.o: programs/rv32/rt/muldiv.c programs/rv32/rt/muldiv.h | build/rv32m
	$(RV32_CC) $(RV32M_CFLAGS) -c -o $@ $<
build/rv32m/digit_weights.o: build/rv32/digit_weights.c build/rv32/digit_weights.h | build/rv32m
	$(RV32_CC) $(RV32M_CFLAGS) -c -o $@ $<
.SECONDEXPANSION:
build/rv32m/%.elf: $$(RV32M_OBJS_$$*) $(RV32M_COMMON_OBJS) programs/rv32/link.ld
	$(RV32M_MARCH_CHECK)$(RV32_CC) $(RV32M_LDFLAGS) -Wl,-Map,$(@:.elf=.map) -o $@ $(filter %.o,$^)
build/rv32m/%.lst: build/rv32m/%.elf
	$(RV32_OBJDUMP) -d -S $< > $@
build/rv32m/%.bin: build/rv32m/%.elf
	$(RV32_OBJCOPY) -O binary $< $@
# Kept after the build (not intermediates to delete): the ELF is what gdb and the checker read.
.SECONDARY: $(foreach image,$(RV32M_IMAGES),build/rv32m/$(image).elf $(RV32M_OBJS_$(image))) $(RV32M_COMMON_OBJS)
firmware-rv32m: toolchain-rv32 $(foreach image,$(RV32M_IMAGES),build/rv32m/$(image).elf build/rv32m/$(image).bin build/rv32m/$(image).lst)
# --require-m: the listing multiplies or divides in hardware and, apart from the self-check,
# no software routine is linked (the self-check keeps it to test it by name).
check-rv32m-image: firmware-rv32m
	$(PYTHON) tools/rv32_image.py build/rv32m/selfcheck.elf --listing build/rv32m/selfcheck.lst --bin build/rv32m/selfcheck.bin --hex build/rv32m/selfcheck.hex --allow-m
	$(PYTHON) tools/rv32_image.py build/rv32m/diag.elf --listing build/rv32m/diag.lst --bin build/rv32m/diag.bin --hex build/rv32m/diag.hex --allow-privileged --require-m
	$(PYTHON) tools/rv32_image.py build/rv32m/pong.elf --listing build/rv32m/pong.lst --bin build/rv32m/pong.bin --hex build/rv32m/pong.hex --require-m
	$(PYTHON) tools/rv32_image.py build/rv32m/capstone.elf --listing build/rv32m/capstone.lst --bin build/rv32m/capstone.bin --hex build/rv32m/capstone.hex --require-m
RV32M_SELFCHECK_ARGS := --image build/rv32m/selfcheck.bin --expect-console "PASS $(RV32_SELFCHECK_HEX)"
RV32M_DIAG_ARGS := $(subst build/rv32/diag.bin,build/rv32m/diag.bin,$(RV32_DIAG_ARGS))
RV32M_PONG_ARGS := $(subst build/rv32/pong.bin,build/rv32m/pong.bin,$(RV32_PONG_ARGS))
RV32M_CAPSTONE_ARGS := $(subst build/rv32/capstone.bin,build/rv32m/capstone.bin,$(RV32_CAPSTONE_ARGS))
run-rv32m-emu: check-rv32m-image $(RV32EMU)
	$(PYTHON) tools/rv32_run_emu.py build/rv32m/selfcheck.bin --emulator $(RV32EMU) --transcript build/rv32m/selfcheck.emu.transcript --trace build/rv32m/selfcheck.trace --state build/rv32m/selfcheck.state --expect-hex $(RV32_SELFCHECK_HEX)
	$(PYTHON) -m tools.rv32_rtl $(RV32M_DIAG_ARGS) --backend emulator --emulator $(RV32EMU) --out build/rv32m/emu
	$(PYTHON) -m tools.rv32_rtl $(RV32M_PONG_ARGS) --backend emulator --emulator $(RV32EMU) --out build/rv32m/emu
	$(PYTHON) -m tools.rv32_rtl $(RV32M_CAPSTONE_ARGS) --backend emulator --emulator $(RV32EMU) --out build/rv32m/emu
# Icarus runs the self-check and the diagnostic, Verilator all four (Pong and the capstone
# with a stalled bus), as the RV32I targets split them.
run-rv32m-rtl: check-rv32m-image $(RV32_TB_VVP) $(RV32EMU)
	$(PYTHON) -m tools.rv32_rtl $(RV32M_SELFCHECK_ARGS) --emulator $(RV32EMU) --simulator $(RV32_TB_VVP) --rtl-timeout $(RV32_ICARUS_TIMEOUT) --out build/rv32m/rtl
	$(PYTHON) -m tools.rv32_rtl $(RV32M_DIAG_ARGS) --emulator $(RV32EMU) --simulator $(RV32_TB_VVP) --rtl-timeout $(RV32_ICARUS_TIMEOUT) --out build/rv32m/rtl
run-rv32m-rtl-verilator: check-rv32m-image $(RV32_TB_VERILATOR) $(RV32EMU)
	$(PYTHON) -m tools.rv32_rtl $(RV32M_SELFCHECK_ARGS) --emulator $(RV32EMU) --simulator $(RV32_TB_VERILATOR) --stall 1 --out build/rv32m/rtl-verilator
	$(PYTHON) -m tools.rv32_rtl $(RV32M_DIAG_ARGS) --emulator $(RV32EMU) --simulator $(RV32_TB_VERILATOR) --out build/rv32m/rtl-verilator
	$(PYTHON) -m tools.rv32_rtl $(RV32M_PONG_ARGS) --emulator $(RV32EMU) --simulator $(RV32_TB_VERILATOR) --stall 1 --out build/rv32m/rtl-verilator
	$(PYTHON) -m tools.rv32_rtl $(RV32M_CAPSTONE_ARGS) --max-cycles 20000000 --emulator $(RV32EMU) --simulator $(RV32_TB_VERILATOR) --stall 1 --out build/rv32m/rtl-verilator
# The directed M and Zicntr tests (tests/test_rv32_m.py) on each simulator.
test-rv32-m: $(RV32EMU)
	HOST_CC=$(HOST_CC) $(PYTHON) -m unittest discover -s tests -p 'test_rv32_m.py' -v
test-rv32-m-verilator: $(RV32EMU) $(RV32_TB_VERILATOR)
	HOST_CC=$(HOST_CC) RV32_RTL_SIM=$(RV32_TB_VERILATOR) $(PYTHON) -m unittest discover -s tests -p 'test_rv32_m.py' -v
test-rv32: test-rv32-m test-rv32-m-verilator run-rv32m-emu run-rv32m-rtl-verilator
test-rv32-slow: run-rv32m-rtl

# Track 0: CoreMark and Dhrystone, each built for RV32I (software multiply and divide) and
# RV32IM, timed with the Zicntr counters. The benchmark sources are vendored unmodified
# (third_party/coremark, third_party/dhrystone) and compiled without -Werror, apart from
# implicit function declarations; the port (programs/rv32/bench) is ours and is.
# docs/rv32-groundwork.md has the record.
.PHONY: firmware-rv32-bench check-rv32-bench-image bench-rv32-emu bench-rv32 test-rv32-bench
RV32_COREMARK_ITERATIONS ?= 30
RV32_BENCH_OPT := -O2
RV32_BENCH_BASE := --target=riscv32-unknown-elf -mabi=ilp32 -mcmodel=medlow -mno-relax -ffreestanding -fno-builtin -nostdlib \
	$(RV32_BENCH_OPT) -g -fno-asynchronous-unwind-tables -fno-unwind-tables
RV32_BENCH_PORT_FLAGS := -std=c11 -Wall -Wextra -Werror -Iprograms/rv32 -Iprograms/rv32/bench -Ithird_party/coremark
RV32_COREMARK_FLAGS := -std=c11 -Werror=implicit-function-declaration -Iprograms/rv32/bench -Ithird_party/coremark -DITERATIONS=$(RV32_COREMARK_ITERATIONS) \
	-DPERFORMANCE_RUN=1 -DCOMPILER_FLAGS='"$(RV32_BENCH_OPT)"'
# riscv-tests' Dhrystone is K&R C that asks not to be inlined (a GCC pragma clang ignores).
# Its old-style definitions and its procedures that fall off the end without a value are
# the two warnings it produces; procs.h declares the routines it calls before defining.
RV32_DHRYSTONE_FLAGS := -std=gnu89 -Wno-deprecated-non-prototype -Wno-return-type -Werror=implicit-function-declaration \
	-include programs/rv32/bench/dhrystone/procs.h -fno-inline -Iprograms/rv32/bench/dhrystone -Ithird_party/dhrystone
RV32_COREMARK_SOURCES := $(addprefix third_party/coremark/,core_list_join.c core_main.c core_matrix.c core_state.c core_util.c)
RV32_BENCH_HEADERS := programs/rv32/bench/bench.h programs/rv32/bench/core_portme.h third_party/coremark/coremark.h \
	$(wildcard programs/rv32/bench/dhrystone/*.h) third_party/dhrystone/dhrystone.h programs/rv32/console.h
RV32_BENCH_IMAGES := coremark-i coremark-im dhrystone-i dhrystone-im
build/rv32bench/i build/rv32bench/im:
	mkdir -p $@
# Every benchmark object depends on this stamp, which holds the compiler and flags and is
# rewritten only when they change: RV32_COREMARK_ITERATIONS=31 rebuilds, a repeat does not.
# A change also deletes the objects, because make 3.81 (macOS) compares whole seconds and
# would keep an object built in the same second as the new stamp.
RV32_BENCH_STAMP := build/rv32bench/flags
RV32_BENCH_STAMP_TEXT := '$(subst ','\'',$(RV32_CC) $(RV32_BENCH_BASE) | $(RV32_BENCH_PORT_FLAGS) | $(RV32_COREMARK_FLAGS) | $(RV32_DHRYSTONE_FLAGS))'
$(RV32_BENCH_STAMP): FORCE | build/rv32bench/i
	@printf '%s\n' $(RV32_BENCH_STAMP_TEXT) | cmp -s - $@ || \
		{ rm -f build/rv32bench/i/*.o build/rv32bench/im/*.o && printf '%s\n' $(RV32_BENCH_STAMP_TEXT) > $@; }
.PHONY: FORCE
FORCE:
# One object set per ISA: `i` links the software multiply/divide, `im` does not need it.
define RV32_BENCH_ISA
build/rv32bench/$(1)/%.o: third_party/coremark/%.c $(RV32_BENCH_HEADERS) $(RV32_BENCH_STAMP) | build/rv32bench/$(1)
	$(RV32_CC) $(RV32_BENCH_BASE) -march=$(2) $(RV32_COREMARK_FLAGS) -c -o $$@ $$<
build/rv32bench/$(1)/%.o: third_party/dhrystone/%.c $(RV32_BENCH_HEADERS) $(RV32_BENCH_STAMP) | build/rv32bench/$(1)
	$(RV32_CC) $(RV32_BENCH_BASE) -march=$(2) $(RV32_DHRYSTONE_FLAGS) -c -o $$@ $$<
build/rv32bench/$(1)/%.o: programs/rv32/bench/%.c $(RV32_BENCH_HEADERS) $(RV32_BENCH_STAMP) | build/rv32bench/$(1)
	$(RV32_CC) $(RV32_BENCH_BASE) -march=$(2) $(RV32_BENCH_PORT_FLAGS) -DITERATIONS=$(RV32_COREMARK_ITERATIONS) -DPERFORMANCE_RUN=1 -c -o $$@ $$<
build/rv32bench/$(1)/port.o: programs/rv32/bench/dhrystone/port.c $(RV32_BENCH_HEADERS) $(RV32_BENCH_STAMP) | build/rv32bench/$(1)
	$(RV32_CC) $(RV32_BENCH_BASE) -march=$(2) $(RV32_BENCH_PORT_FLAGS) -c -o $$@ $$<
build/rv32bench/$(1)/%.o: programs/rv32/%.S programs/rv32/board.h $(RV32_BENCH_STAMP) | build/rv32bench/$(1)
	$(RV32_CC) $(RV32_BENCH_BASE) -march=$(2) $(RV32_BENCH_PORT_FLAGS) -c -o $$@ $$<
build/rv32bench/$(1)/%.o: programs/rv32/%.c $(RV32_HEADERS) $(RV32_BENCH_STAMP) | build/rv32bench/$(1)
	$(RV32_CC) $(RV32_BENCH_BASE) -march=$(2) $(RV32_BENCH_PORT_FLAGS) -c -o $$@ $$<
build/rv32bench/$(1)/muldiv.o: programs/rv32/rt/muldiv.c programs/rv32/rt/muldiv.h $(RV32_BENCH_STAMP) | build/rv32bench/$(1)
	$(RV32_CC) $(RV32_BENCH_BASE) -march=$(2) $(RV32_BENCH_PORT_FLAGS) -c -o $$@ $$<
build/rv32bench/coremark-$(1).elf: $(patsubst third_party/coremark/%.c,build/rv32bench/$(1)/%.o,$(RV32_COREMARK_SOURCES)) \
		build/rv32bench/$(1)/core_portme.o build/rv32bench/$(1)/bench.o build/rv32bench/$(1)/start.o build/rv32bench/$(1)/console.o $(3) programs/rv32/link.ld
	$(RV32_CC) $(RV32_BENCH_BASE) -march=$(2) -static --ld-path=$(RV32_LD) -Wl,-T,programs/rv32/link.ld -Wl,-Map,$$(@:.elf=.map) -o $$@ $$(filter %.o,$$^)
build/rv32bench/dhrystone-$(1).elf: build/rv32bench/$(1)/dhrystone.o build/rv32bench/$(1)/dhrystone_main.o build/rv32bench/$(1)/port.o \
		build/rv32bench/$(1)/bench.o build/rv32bench/$(1)/start.o build/rv32bench/$(1)/console.o $(3) programs/rv32/link.ld
	$(RV32_CC) $(RV32_BENCH_BASE) -march=$(2) -static --ld-path=$(RV32_LD) -Wl,-T,programs/rv32/link.ld -Wl,--wrap=debug_printf -Wl,-Map,$$(@:.elf=.map) -o $$@ $$(filter %.o,$$^)
endef
$(eval $(call RV32_BENCH_ISA,i,rv32i,build/rv32bench/i/muldiv.o))
$(eval $(call RV32_BENCH_ISA,im,rv32im,))
build/rv32bench/%.lst: build/rv32bench/%.elf
	$(RV32_OBJDUMP) -d -S $< > $@
build/rv32bench/%.bin: build/rv32bench/%.elf
	$(RV32_OBJCOPY) -O binary $< $@
.SECONDARY: $(foreach image,$(RV32_BENCH_IMAGES),build/rv32bench/$(image).elf)
firmware-rv32-bench: toolchain-rv32 $(foreach image,$(RV32_BENCH_IMAGES),build/rv32bench/$(image).bin build/rv32bench/$(image).lst)
check-rv32-bench-image: firmware-rv32-bench
	$(PYTHON) tools/rv32_image.py build/rv32bench/coremark-i.elf --listing build/rv32bench/coremark-i.lst --bin build/rv32bench/coremark-i.bin --hex build/rv32bench/coremark-i.hex --allow-counters
	$(PYTHON) tools/rv32_image.py build/rv32bench/coremark-im.elf --listing build/rv32bench/coremark-im.lst --bin build/rv32bench/coremark-im.bin --hex build/rv32bench/coremark-im.hex --allow-counters --require-m
	$(PYTHON) tools/rv32_image.py build/rv32bench/dhrystone-i.elf --listing build/rv32bench/dhrystone-i.lst --bin build/rv32bench/dhrystone-i.bin --hex build/rv32bench/dhrystone-i.hex --allow-counters
	$(PYTHON) tools/rv32_image.py build/rv32bench/dhrystone-im.elf --listing build/rv32bench/dhrystone-im.lst --bin build/rv32bench/dhrystone-im.bin --hex build/rv32bench/dhrystone-im.hex --allow-counters --require-m
RV32_BENCH_BINS := $(foreach image,$(RV32_BENCH_IMAGES),build/rv32bench/$(image).bin)
# The emulator validates the four images and pins instret; its "cycles" are instructions.
bench-rv32-emu: check-rv32-bench-image $(RV32EMU)
	$(PYTHON) tools/rv32_bench.py $(RV32_BENCH_BINS) --backend emulator --emulator $(RV32EMU)
# The performance baseline: clock cycles on the RTL (Verilator; Icarus counts the same cycles,
# slowly), with the emulator alongside to require the same console and instret.
bench-rv32: check-rv32-bench-image $(RV32EMU) $(RV32_TB_VERILATOR)
	$(PYTHON) tools/rv32_bench.py $(RV32_BENCH_BINS) --backend emulator --backend verilator --emulator $(RV32EMU) --verilator $(RV32_TB_VERILATOR)
test-rv32-bench:
	$(PYTHON) -m unittest discover -s tests -p 'test_rv32_bench.py' -v
test-rv32: test-rv32-bench bench-rv32-emu

# Track 0: architectural compliance. riscv-arch-test is fetched at the pinned commit (only the
# model headers and the I, M and F suites); each test's signature must match QEMU's and the
# emulator's trace must match each simulator's. The QEMU CPU is generic RV32 with C and D off.
.PHONY: fetch-rv32-arch-test test-rv32-arch-model test-rv32-arch test-rv32-arch-verilator test-rv32-arch-icarus
RV32_ARCH_QEMU_CPU ?= rv32,c=false,d=false
RV32_ARCH_JOBS ?= 4
RV32_ARCH_ARGS = --cc $(RV32_CC) --ld $(RV32_LD) --qemu $(QEMU_RV32) --qemu-cpu $(RV32_ARCH_QEMU_CPU) --emulator $(RV32EMU) --jobs $(RV32_ARCH_JOBS)
fetch-rv32-arch-test:
	$(PYTHON) tools/rv32_arch_test.py --fetch
# The model header's own behaviour (signature dump, the mstatus skip, failures), no suite needed.
test-rv32-arch-model: toolchain-rv32 $(RV32EMU)
	RV32_CC=$(RV32_CC) RV32_LD=$(RV32_LD) QEMU_RV32=$(QEMU_RV32) RV32_ARCH_QEMU_CPU=$(RV32_ARCH_QEMU_CPU) $(PYTHON) -m unittest discover -s tests -p 'test_rv32_arch.py' -v
test-rv32-arch: toolchain-rv32 fetch-rv32-arch-test $(RV32EMU)
	$(PYTHON) tools/rv32_arch_test.py $(RV32_ARCH_ARGS) --backend qemu --out build/rv32/arch/emu
test-rv32-arch-verilator: toolchain-rv32 fetch-rv32-arch-test $(RV32EMU) $(RV32_TB_VERILATOR)
	$(PYTHON) tools/rv32_arch_test.py $(RV32_ARCH_ARGS) --backend verilator --verilator $(RV32_TB_VERILATOR) --out build/rv32/arch/verilator
# About an hour on four cores: the F suite alone retires 19.5 million instructions.
test-rv32-arch-icarus: toolchain-rv32 fetch-rv32-arch-test $(RV32EMU) $(RV32_TB_VVP)
	$(PYTHON) tools/rv32_arch_test.py $(RV32_ARCH_ARGS) --backend icarus --icarus $(RV32_TB_VVP) --out build/rv32/arch/icarus
test-rv32: test-rv32-arch-model test-rv32-arch test-rv32-arch-verilator

# Track 1: a virt-compatible platform (docs/rv32-platform.md). The machine's device tree comes
# from tools/rv32_dtb.py, which also writes the committed C array and boot ROM; platcheck reads
# whatever tree a1 points at and runs unmodified on QEMU virt, the emulator and the RTL.
.PHONY: check-rv32-dtb check-rv32-virt-map check-rv32-platcheck-image test-rv32-platform
.PHONY: run-rv32-platform-qemu run-rv32-platform-emu run-rv32-platform-rtl run-rv32-platform-rtl-verilator
RV32_PLATCHECK_OBJS := build/rv32/platcheck.o build/rv32/fdt.o $(RV32_COMMON_OBJS)
RV32_PLATCHECK_HEX := b8a59113
# QEMU 8.2 has no bare `rv32i` model; the generic CPU runs the RV32I image as is.
RV32_PLATFORM_QEMU_CPU ?= rv32
RV32_PLATFORM_ARGS := --image build/rv32/platcheck.bin --compare results --expect-last-line "PASS $(RV32_PLATCHECK_HEX)" --expect-console-file programs/rv32/platcheck.expected

build/rv32/fdt.o build/rv32/platcheck.o: programs/rv32/fdt.h

build/rv32/platcheck.elf: $(RV32_PLATCHECK_OBJS) programs/rv32/link.ld
	$(RV32_CC) $(RV32_LDFLAGS) -Wl,-Map,$(@:.elf=.map) -o $@ $(RV32_PLATCHECK_OBJS)

check-rv32-dtb:
	$(PYTHON) tools/rv32_dtb.py --check --dtb build/rv32/machine.dtb

check-rv32-virt-map:
	$(PYTHON) tools/rv32_virt_map.py --qemu $(QEMU_RV32)

check-rv32-platcheck-image: build/rv32/platcheck.elf build/rv32/platcheck.lst build/rv32/platcheck.bin
	$(PYTHON) tools/rv32_image.py build/rv32/platcheck.elf --listing build/rv32/platcheck.lst --bin build/rv32/platcheck.bin --hex build/rv32/platcheck.hex --allow-privileged --allow-counters

run-rv32-platform-qemu: check-rv32-platcheck-image
	$(PYTHON) tools/rv32_run_qemu.py build/rv32/platcheck.elf --qemu $(QEMU_RV32) --cpu $(RV32_PLATFORM_QEMU_CPU) --last-line --timeout 20 --expect-hex $(RV32_PLATCHECK_HEX) --transcript build/rv32/platcheck.qemu.transcript --qemu-log build/rv32/platcheck.qemu.log
	diff -u programs/rv32/platcheck.qemu.expected build/rv32/platcheck.qemu.transcript

run-rv32-platform-emu: check-rv32-platcheck-image $(RV32EMU)
	$(PYTHON) -m tools.rv32_rtl $(RV32_PLATFORM_ARGS) --backend emulator --emulator $(RV32EMU) --out build/rv32/platform-emu

run-rv32-platform-rtl: check-rv32-platcheck-image $(RV32EMU) $(RV32_TB_VVP)
	$(PYTHON) -m tools.rv32_rtl $(RV32_PLATFORM_ARGS) --emulator $(RV32EMU) --simulator $(RV32_TB_VVP) --rtl-timeout $(RV32_ICARUS_TIMEOUT) --out build/rv32/platform-icarus

run-rv32-platform-rtl-verilator: check-rv32-platcheck-image $(RV32EMU) $(RV32_TB_VERILATOR)
	$(PYTHON) -m tools.rv32_rtl $(RV32_PLATFORM_ARGS) --emulator $(RV32EMU) --simulator $(RV32_TB_VERILATOR) --stall 1 --out build/rv32/platform-verilator

test-rv32-platform: check-rv32-platcheck-image
	HOST_CC=$(HOST_CC) QEMU_RV32=$(QEMU_RV32) $(PYTHON) -m unittest discover -s tests -p 'test_rv32_platform.py' -v

test-rv32: check-rv32-dtb check-rv32-virt-map test-rv32-platform run-rv32-platform-qemu run-rv32-platform-emu run-rv32-platform-rtl-verilator
test-rv32-slow: run-rv32-platform-rtl

# Track 2 (docs/rv32-os.md, plan docs/planning/track2-os.md). O1: interrupts. irqcheck takes
# CLINT and PLIC interrupts and runs unmodified on QEMU virt, the emulator and the RTL; the RTL is
# compared at the results level with cycle ticks and trace for trace in step-tick mode.
.PHONY: check-rv32-irqcheck-image run-rv32-irq-qemu run-rv32-irq-emu run-rv32-irq-rtl run-rv32-irq-rtl-verilator run-rv32-irq-rtl-steps test-rv32-irq test-rv32-pmp test-rv32-irq-icarus test-rv32-pmp-icarus
RV32_IRQCHECK_OBJS := build/rv32/irqcheck.o build/rv32/trap.o build/rv32/fdt.o $(RV32_COMMON_OBJS)
RV32_IRQCHECK_HEX := 133cab46
RV32_IRQ_ARGS := --image build/rv32/irqcheck.bin --input programs/rv32/irqcheck.input --expect-last-line "PASS $(RV32_IRQCHECK_HEX)" --expect-console-file programs/rv32/irqcheck.expected

build/rv32/irqcheck.o: programs/rv32/csr.h programs/rv32/fdt.h programs/rv32/clint.h

build/rv32/irqcheck.elf: $(RV32_IRQCHECK_OBJS) programs/rv32/link.ld
	$(RV32_CC) $(RV32_LDFLAGS) -Wl,-Map,$(@:.elf=.map) -o $@ $(RV32_IRQCHECK_OBJS)

check-rv32-irqcheck-image: build/rv32/irqcheck.elf build/rv32/irqcheck.lst build/rv32/irqcheck.bin
	$(PYTHON) tools/rv32_image.py build/rv32/irqcheck.elf --listing build/rv32/irqcheck.lst --bin build/rv32/irqcheck.bin --hex build/rv32/irqcheck.hex --allow-system

run-rv32-irq-qemu: check-rv32-irqcheck-image
	$(PYTHON) tools/rv32_run_qemu.py build/rv32/irqcheck.elf --qemu $(QEMU_RV32) --cpu $(RV32_PLATFORM_QEMU_CPU) --last-line --timeout 20 --expect-hex $(RV32_IRQCHECK_HEX) --transcript build/rv32/irqcheck.qemu.transcript --qemu-log build/rv32/irqcheck.qemu.log
	diff -u programs/rv32/irqcheck.qemu.expected build/rv32/irqcheck.qemu.transcript

run-rv32-irq-emu: check-rv32-irqcheck-image $(RV32EMU)
	$(PYTHON) -m tools.rv32_rtl $(RV32_IRQ_ARGS) --compare results --backend emulator --emulator $(RV32EMU) --out build/rv32/irq-emu

run-rv32-irq-rtl: check-rv32-irqcheck-image $(RV32EMU) $(RV32_TB_VVP)
	$(PYTHON) -m tools.rv32_rtl $(RV32_IRQ_ARGS) --compare results --emulator $(RV32EMU) --simulator $(RV32_TB_VVP) --rtl-timeout $(RV32_ICARUS_TIMEOUT) --out build/rv32/irq-icarus

run-rv32-irq-rtl-verilator: check-rv32-irqcheck-image $(RV32EMU) $(RV32_TB_VERILATOR)
	$(PYTHON) -m tools.rv32_rtl $(RV32_IRQ_ARGS) --compare results --emulator $(RV32EMU) --simulator $(RV32_TB_VERILATOR) --stall 1 --out build/rv32/irq-verilator

# Step ticks: the same program, trace-identical with the emulator, interrupt lines included.
run-rv32-irq-rtl-steps: check-rv32-irqcheck-image $(RV32EMU) $(RV32_TB_VERILATOR)
	$(PYTHON) -m tools.rv32_rtl $(RV32_IRQ_ARGS) --ticks steps --allow-traps --emulator $(RV32EMU) --simulator $(RV32_TB_VERILATOR) --seed 5 --out build/rv32/irq-steps

# Issue #20: S-mode and Sv32. mmucheck runs on QEMU (with Svade, so a clear A or D bit faults as
# ours does), the emulator and the RTL; tests/test_rv32_mmu.py is the directed half.
.PHONY: check-rv32-mmucheck-image run-rv32-mmu-qemu run-rv32-mmu-emu run-rv32-mmu-rtl run-rv32-mmu-rtl-verilator run-rv32-mmu-rtl-steps test-rv32-mmu test-rv32-mmu-icarus
RV32_MMUCHECK_OBJS := build/rv32/mmucheck.o $(RV32_COMMON_OBJS)
RV32_MMUCHECK_HEX := 7530cb0f
RV32_MMU_QEMU_CPU ?= rv32,svade=on,svadu=off
RV32_MMU_ARGS := --image build/rv32/mmucheck.bin --expect-last-line "PASS $(RV32_MMUCHECK_HEX)" --expect-console-file programs/rv32/mmucheck.expected

build/rv32/mmucheck.o: programs/rv32/csr.h

build/rv32/mmucheck.elf: $(RV32_MMUCHECK_OBJS) programs/rv32/link.ld
	$(RV32_CC) $(RV32_LDFLAGS) -Wl,-Map,$(@:.elf=.map) -o $@ $(RV32_MMUCHECK_OBJS)

check-rv32-mmucheck-image: build/rv32/mmucheck.elf build/rv32/mmucheck.lst build/rv32/mmucheck.bin
	$(PYTHON) tools/rv32_image.py build/rv32/mmucheck.elf --listing build/rv32/mmucheck.lst --bin build/rv32/mmucheck.bin --hex build/rv32/mmucheck.hex --allow-system

run-rv32-mmu-qemu: check-rv32-mmucheck-image
	$(PYTHON) tools/rv32_run_qemu.py build/rv32/mmucheck.elf --qemu $(QEMU_RV32) --cpu $(RV32_MMU_QEMU_CPU) --last-line --timeout 20 --expect-hex $(RV32_MMUCHECK_HEX) --transcript build/rv32/mmucheck.qemu.transcript --qemu-log build/rv32/mmucheck.qemu.log
	diff -u programs/rv32/mmucheck.expected build/rv32/mmucheck.qemu.transcript

run-rv32-mmu-emu: check-rv32-mmucheck-image $(RV32EMU)
	$(PYTHON) -m tools.rv32_rtl $(RV32_MMU_ARGS) --compare results --backend emulator --emulator $(RV32EMU) --out build/rv32/mmu-emu

run-rv32-mmu-rtl: check-rv32-mmucheck-image $(RV32EMU) $(RV32_TB_VVP)
	$(PYTHON) -m tools.rv32_rtl $(RV32_MMU_ARGS) --compare results --emulator $(RV32EMU) --simulator $(RV32_TB_VVP) --rtl-timeout $(RV32_ICARUS_TIMEOUT) --out build/rv32/mmu-icarus

run-rv32-mmu-rtl-verilator: check-rv32-mmucheck-image $(RV32EMU) $(RV32_TB_VERILATOR)
	$(PYTHON) -m tools.rv32_rtl $(RV32_MMU_ARGS) --compare results --emulator $(RV32EMU) --simulator $(RV32_TB_VERILATOR) --stall 1 --out build/rv32/mmu-verilator

run-rv32-mmu-rtl-steps: check-rv32-mmucheck-image $(RV32EMU) $(RV32_TB_VERILATOR)
	$(PYTHON) -m tools.rv32_rtl $(RV32_MMU_ARGS) --ticks steps --allow-traps --emulator $(RV32EMU) --simulator $(RV32_TB_VERILATOR) --seed 5 --out build/rv32/mmu-steps

test-rv32-mmu: $(RV32EMU) $(RV32_TB_VERILATOR)
	HOST_CC=$(HOST_CC) RV32_RTL_SIM=$(RV32_TB_VERILATOR) $(PYTHON) -m unittest discover -s tests -p 'test_rv32_mmu.py' -v
test-rv32-mmu-icarus: $(RV32EMU) $(RV32_TB_VVP)
	HOST_CC=$(HOST_CC) RV32_RTL_SIM=$(RV32_TB_VVP) $(PYTHON) -m unittest discover -s tests -p 'test_rv32_mmu.py' -v
test-rv32: run-rv32-mmu-qemu run-rv32-mmu-emu run-rv32-mmu-rtl run-rv32-mmu-rtl-verilator run-rv32-mmu-rtl-steps test-rv32-mmu test-rv32-mmu-icarus

test-rv32-irq: $(RV32EMU) $(RV32_TB_VERILATOR)
	HOST_CC=$(HOST_CC) RV32_RTL_SIM=$(RV32_TB_VERILATOR) $(PYTHON) -m unittest discover -s tests -p 'test_rv32_irq.py' -v

# O5: user mode and PMP, directed, emulator against the RTL in step-tick mode.
test-rv32-pmp: $(RV32EMU) $(RV32_TB_VERILATOR)
	HOST_CC=$(HOST_CC) RV32_RTL_SIM=$(RV32_TB_VERILATOR) $(PYTHON) -m unittest discover -s tests -p 'test_rv32_pmp.py' -v
# The same directed tests on Icarus.
test-rv32-irq-icarus: $(RV32EMU) $(RV32_TB_VVP)
	HOST_CC=$(HOST_CC) RV32_RTL_SIM=$(RV32_TB_VVP) $(PYTHON) -m unittest discover -s tests -p 'test_rv32_irq.py' -v
test-rv32-pmp-icarus: $(RV32EMU) $(RV32_TB_VVP)
	HOST_CC=$(HOST_CC) RV32_RTL_SIM=$(RV32_TB_VVP) $(PYTHON) -m unittest discover -s tests -p 'test_rv32_pmp.py' -v

test-rv32: run-rv32-irq-qemu run-rv32-irq-emu run-rv32-irq-rtl run-rv32-irq-rtl-verilator run-rv32-irq-rtl-steps test-rv32-irq test-rv32-pmp test-rv32-irq-icarus test-rv32-pmp-icarus

# O2: a kernel with system calls. Each program is linked for its own slots (128 KiB each since O4)
# above the kernel (programs/rv32/os/user.ld), packed onto a RAM disk (tools/rv32_ramdisk.py) and bundled
# into the kernel image, which boots the shell. A console session is scripted with
# --console-input (the emulator), +console-input= (the testbench) and stdin (QEMU).
.PHONY: firmware-rv32-os check-rv32-os-image run-rv32-os-qemu run-rv32-os-emu run-rv32-os-rtl run-rv32-os-rtl-verilator
.PHONY: run-rv32-os-pong-emu run-rv32-os-pong-rtl-steps run-rv32-os-boot2 run-rv32-os-menu-emu run-rv32-os-menu-rtl-verilator test-rv32-os
.PHONY: run-rv32-os-qemu-reboot print-rv32-os-layout
RV32_OS := programs/rv32/os
RV32_OS_CFLAGS := $(RV32_CFLAGS) -I$(RV32_OS) -Ibuild/rv32
RV32_OS_HEADERS := $(RV32_OS)/sys.h $(RV32_OS)/ulib.h $(RV32_OS)/udecimal.h $(RV32_OS)/fs.h $(RV32_OS)/virtio.h $(RV32_OS)/score.h $(RV32_OS)/report.h programs/rv32/csr.h programs/rv32/fdt.h programs/rv32/clint.h programs/rv32/virtio_mmio.h $(RV32_HEADERS)
RV32_OS_PROGRAMS := sh hello primes pong tetris menu syscheck fault cat write files bars life fill dmaprobe
# Slots of 128 KiB from 0x8010_0000 (programs/rv32/os/sys.h and tools/rv32_ramdisk.py, which
# test-rv32-os holds to these values); a program's span is 1 slot unless given.
RV32_OS_SLOT_BASE := 0x80100000
RV32_OS_SLOT_SIZE := 0x20000
RV32_OS_SLOT_sh := 0
RV32_OS_SLOT_hello := 1
RV32_OS_SLOT_primes := 2
RV32_OS_SLOT_pong := 3
RV32_OS_SLOT_tetris := 4
# The menu keeps its G2 depth buffer (150 KiB) in its own slots, where the DMA window lets G2
# reach (issue #20), so it needs three; of its old slots, 5 went to dmaprobe and 6 is free.
RV32_OS_SLOT_menu := 15
RV32_OS_SPAN_menu := 3
RV32_OS_SLOT_dmaprobe := 5
RV32_OS_SLOT_syscheck := 7
RV32_OS_SLOT_fault := 8
RV32_OS_SLOT_cat := 9
RV32_OS_SLOT_write := 10
RV32_OS_SLOT_files := 11
RV32_OS_SLOT_bars := 12
RV32_OS_SLOT_life := 13
RV32_OS_SLOT_fill := 14
rv32_os_span = $(or $(RV32_OS_SPAN_$(1)),1)
# A program's load address and span in bytes: the one place the slot formula is written here.
rv32_os_base = $$(printf '0x%x' $$(($(RV32_OS_SLOT_BASE) + $(RV32_OS_SLOT_$(1)) * $(RV32_OS_SLOT_SIZE))))
rv32_os_size = $$(printf '0x%x' $$(($(call rv32_os_span,$(1)) * $(RV32_OS_SLOT_SIZE))))
# Programs run in user mode (O5), so their images may hold only what user mode may run; fault
# reads mstatus on purpose, to be killed for it.
rv32_os_gate = $(or $(RV32_OS_GATE_$(1)),--allow-user)
RV32_OS_GATE_fault := --allow-system
RV32_OS_USER := build/rv32/os/ustart.o build/rv32/os/ulib.o build/rv32/os/udecimal.o build/rv32/os/mem.o build/rv32/muldiv.o
RV32_OS_OBJS_sh := build/rv32/os/sh.o
RV32_OS_OBJS_hello := build/rv32/os/hello.o
RV32_OS_OBJS_primes := build/rv32/os/primes.o
RV32_OS_OBJS_syscheck := build/rv32/os/syscheck.o
RV32_OS_OBJS_fault := build/rv32/os/fault.o
RV32_OS_OBJS_cat := build/rv32/os/cat.o
RV32_OS_OBJS_write := build/rv32/os/write.o
RV32_OS_OBJS_files := build/rv32/os/files.o
RV32_OS_OBJS_bars := build/rv32/os/bars.o build/rv32/os/report.o
RV32_OS_OBJS_life := build/rv32/os/life.o build/rv32/os/report.o
RV32_OS_OBJS_fill := build/rv32/os/fill.o
RV32_OS_OBJS_dmaprobe := build/rv32/os/dmaprobe.o
RV32_OS_OBJS_pong := build/rv32/os/pong.o build/rv32/os/score.o build/rv32/pong_game.o build/rv32/gfx.o
RV32_OS_OBJS_tetris := build/rv32/os/tetris.o build/rv32/os/score.o build/rv32/tetris_game.o build/rv32/gfx.o build/rv32/gfx_text.o
RV32_OS_OBJS_menu := build/rv32/os/menu.o $(filter-out build/rv32/capstone.o $(RV32_COMMON_OBJS),$(RV32_CAPSTONE_OBJS))
RV32_OS_ELFS := $(foreach p,$(RV32_OS_PROGRAMS),build/rv32/os/$(p).elf)
RV32_OS_KERNEL_OBJS := build/rv32/os/kentry.o build/rv32/os/kernel.o build/rv32/os/mem.o build/rv32/os/virtio.o build/rv32/os/fs.o build/rv32/fdt.o build/rv32/muldiv.o
RV32_OS_DISK := build/rv32/os/disk.img
RV32_OS_ARGS := --image build/rv32/os/kernel.bin --console-input $(RV32_OS)/session.txt --disk $(RV32_OS_DISK) --compare results --compare-traps faults --expect-console-file $(RV32_OS)/session.expected
RV32_OS_PONG_ARGS := --image build/rv32/os/kernel.bin --console-input $(RV32_OS)/pong.session --input $(RV32_PONG_INPUT) --disk $(RV32_OS_DISK) \
	--expect-checkpoints $(RV32_PONG_EXPECTED) --expect-console-file $(RV32_OS)/pong.session.expected --timeout 600

build/rv32/os:
	mkdir -p $@
# Static pattern rules: GNU Make 3.81 (macOS's /usr/bin/make) takes the first pattern rule that
# matches, not the most specific, so a plain `build/rv32/os/%.o` rule would lose to
# `build/rv32/%.o` above and compile the OS sources without their flags and headers.
RV32_OS_C_OBJS := $(patsubst $(RV32_OS)/%.c,build/rv32/os/%.o,$(wildcard $(RV32_OS)/*.c))
$(RV32_OS_C_OBJS): build/rv32/os/%.o: $(RV32_OS)/%.c $(RV32_OS_HEADERS) $(RV32_DIGIT_GENERATED) $(RV32_G3D_GENERATED) | build/rv32/os
	$(RV32_CC) $(RV32_OS_CFLAGS) -c -o $@ $<
build/rv32/os/ustart.o: $(RV32_OS)/ustart.S $(RV32_OS)/sys.h | build/rv32/os
	$(RV32_CC) $(RV32_OS_CFLAGS) -c -o $@ $<
.SECONDEXPANSION:
$(RV32_OS_ELFS): build/rv32/os/%.elf: $$(RV32_OS_OBJS_$$*) $(RV32_OS_USER) $(RV32_OS)/user.ld
	$(RV32_CC) $(RV32_ARCH) -nostdlib -static --ld-path=$(RV32_LD) -Wl,-T,$(RV32_OS)/user.ld \
		-Wl,--defsym=SLOT_BASE=$(call rv32_os_base,$*) -Wl,--defsym=SLOT_SPAN=$(call rv32_os_size,$*) \
		-Wl,-Map,$(@:.elf=.map) -o $@ $(RV32_OS_OBJS_$*) $(RV32_OS_USER)
$(RV32_OS_ELFS:.elf=.lst) build/rv32/os/kernel.lst: build/rv32/os/%.lst: build/rv32/os/%.elf
	$(RV32_OBJDUMP) -d -S $< > $@
build/rv32/os/kernel.bin: build/rv32/os/%.bin: build/rv32/os/%.elf
	$(RV32_OBJCOPY) -O binary $< $@
build/rv32/os/ramdisk.img: $(RV32_OS_ELFS) tools/rv32_ramdisk.py
	$(PYTHON) tools/rv32_ramdisk.py --out $@ --accelerators menu --accelerators dmaprobe $(RV32_OS_ELFS)
build/rv32/os/kentry.o: $(RV32_OS)/kentry.S build/rv32/os/ramdisk.img programs/rv32/board.h | build/rv32/os
	$(RV32_CC) $(RV32_OS_CFLAGS) -DRAMDISK_IMAGE='"build/rv32/os/ramdisk.img"' -c -o $@ $<
build/rv32/os/kernel.elf: $(RV32_OS_KERNEL_OBJS) $(RV32_OS)/kernel.ld
	$(RV32_CC) $(RV32_ARCH) -nostdlib -static --ld-path=$(RV32_LD) -Wl,-T,$(RV32_OS)/kernel.ld -Wl,-Map,$(@:.elf=.map) -o $@ $(RV32_OS_KERNEL_OBJS)
.SECONDARY: $(RV32_OS_ELFS) build/rv32/os/kernel.elf $(RV32_OS_USER) $(RV32_OS_KERNEL_OBJS) \
	$(sort $(foreach p,$(RV32_OS_PROGRAMS),$(RV32_OS_OBJS_$(p))))

firmware-rv32-os: build/rv32/os/kernel.elf build/rv32/os/kernel.bin build/rv32/os/kernel.lst $(RV32_OS_ELFS:.elf=.lst)

# Every program is checked against its slots, the kernel against its 1 MiB.
# O3: the disk the sessions start from, a tfs file system holding welcome and the two report files
# of O4.
$(RV32_OS_DISK): tools/rv32_mkfs.py $(RV32_OS)/welcome.txt | build/rv32/os
	$(PYTHON) tools/rv32_mkfs.py --new --add welcome=$(RV32_OS)/welcome.txt --add bars.out=/dev/null --add life.out=/dev/null $@
check-rv32-os-image: firmware-rv32-os $(RV32_OS_DISK)
	@set -e; $(foreach p,$(RV32_OS_PROGRAMS),\
		$(PYTHON) tools/rv32_image.py build/rv32/os/$(p).elf --listing build/rv32/os/$(p).lst --ram-base $(call rv32_os_base,$(p)) \
			--ram-size $(call rv32_os_size,$(p)) $(call rv32_os_gate,$(p)) > /dev/null; \
		echo "build/rv32/os/$(p).elf: slot at $(call rv32_os_base,$(p))";)
	$(PYTHON) tools/rv32_image.py build/rv32/os/kernel.elf --listing build/rv32/os/kernel.lst --bin build/rv32/os/kernel.bin --hex build/rv32/os/kernel.hex --ram-size 0x100000 --allow-system
	$(PYTHON) tools/rv32_ramdisk.py --list build/rv32/os/ramdisk.img
print-rv32-os-slot-%:
	@echo $(RV32_OS_SLOT_$*) $(call rv32_os_span,$*)
print-rv32-os-layout:
	@echo $(RV32_OS_SLOT_BASE) $(RV32_OS_SLOT_SIZE)

# QEMU's mtime follows host time unless told otherwise, and a fast host then finishes a job inside
# the kernel's 100 us quantum ("preempted no"). The OS sessions count instructions instead: 2^3 ns
# of virtual time each makes the quantum 12,500 instructions, near the emulator's 10,000 (issue #20).
RV32_OS_QEMU_ICOUNT ?= 3
run-rv32-os-qemu: check-rv32-os-image
	cp $(RV32_OS_DISK) build/rv32/os/session.qemu.disk
	$(PYTHON) tools/rv32_run_qemu.py build/rv32/os/kernel.elf --qemu $(QEMU_RV32) --cpu $(RV32_PLATFORM_QEMU_CPU) --icount $(RV32_OS_QEMU_ICOUNT) --stdin $(RV32_OS)/session.txt --drive build/rv32/os/session.qemu.disk --last-line --timeout 30 --transcript build/rv32/os/session.qemu.transcript
	diff -u $(RV32_OS)/session.qemu.expected build/rv32/os/session.qemu.transcript
# O3 on QEMU as well: the disk QEMU's session left is byte for byte the emulator's, and QEMU boots
# from it again and finds what the session wrote.
run-rv32-os-qemu-reboot: run-rv32-os-qemu run-rv32-os-emu
	cmp build/rv32/os/session.qemu.disk build/rv32/os/emu/kernel.emu.disk
	$(PYTHON) tools/rv32_run_qemu.py build/rv32/os/kernel.elf --qemu $(QEMU_RV32) --cpu $(RV32_PLATFORM_QEMU_CPU) --icount $(RV32_OS_QEMU_ICOUNT) --stdin $(RV32_OS)/reboot.session --drive build/rv32/os/session.qemu.disk --last-line --timeout 30 --transcript build/rv32/os/reboot.qemu.transcript
	diff -u $(RV32_OS)/reboot.session.qemu.expected build/rv32/os/reboot.qemu.transcript
	test "$$($(PYTHON) tools/rv32_mkfs.py build/rv32/os/session.qemu.disk --cat note)" = hi
run-rv32-os-emu: check-rv32-os-image $(RV32EMU)
	$(PYTHON) -m tools.rv32_rtl $(RV32_OS_ARGS) --backend emulator --emulator $(RV32EMU) --out build/rv32/os/emu
run-rv32-os-rtl: check-rv32-os-image $(RV32EMU) $(RV32_TB_VVP)
	$(PYTHON) -m tools.rv32_rtl $(RV32_OS_ARGS) --emulator $(RV32EMU) --simulator $(RV32_TB_VVP) --rtl-timeout $(RV32_ICARUS_TIMEOUT) --max-cycles 50000000 --out build/rv32/os/icarus
run-rv32-os-rtl-verilator: check-rv32-os-image $(RV32EMU) $(RV32_TB_VERILATOR)
	$(PYTHON) -m tools.rv32_rtl $(RV32_OS_ARGS) --emulator $(RV32EMU) --simulator $(RV32_TB_VERILATOR) --stall 1 --max-cycles 50000000 --out build/rv32/os/verilator
# Pong under the kernel gives the standalone image's 200 checkpoints and PASS word, trace for trace
# between the emulator and Verilator in step-tick mode.
run-rv32-os-pong-emu: check-rv32-os-image $(RV32EMU)
	$(PYTHON) -m tools.rv32_rtl $(RV32_OS_PONG_ARGS) --compare results --backend emulator --emulator $(RV32EMU) --out build/rv32/os/pong-emu
run-rv32-os-pong-rtl-steps: check-rv32-os-image $(RV32EMU) $(RV32_TB_VERILATOR)
	$(PYTHON) -m tools.rv32_rtl $(RV32_OS_PONG_ARGS) --ticks steps --allow-traps --emulator $(RV32EMU) --simulator $(RV32_TB_VERILATOR) --max-cycles 60000000 --out build/rv32/os/pong-steps --disk-out build/rv32/os/pong.disk
# A second boot on the disk the Pong session left (O3): the score is still there, on both backends,
# and the host tool reads the same file.
run-rv32-os-boot2: run-rv32-os-pong-rtl-steps
	$(PYTHON) -m tools.rv32_rtl --image build/rv32/os/kernel.bin --console-input $(RV32_OS)/boot2.session --disk build/rv32/os/pong.disk \
		--compare results --compare-traps faults --expect-console-file $(RV32_OS)/boot2.session.expected --emulator $(RV32EMU) --simulator $(RV32_TB_VERILATOR) --out build/rv32/os/boot2
	$(PYTHON) tools/rv32_mkfs.py build/rv32/os/pong.disk --cat scores | diff -u $(RV32_OS)/scores.expected -
# The S1 menu session, the menu run from the shell: S1's 185 checkpoints and PASS word, on the
# emulator and on Verilator at the results level (it drives the accelerators).
RV32_OS_MENU_ARGS := --image build/rv32/os/kernel.bin --console-input $(RV32_OS)/menu.session --input programs/rv32/soc.input \
	--expect-checkpoints programs/rv32/soc.expected --expect-console-file $(RV32_OS)/menu.session.expected --compare results --compare-traps faults --limit 2000000000 --timeout 3600
run-rv32-os-menu-emu: check-rv32-os-image $(RV32EMU)
	$(PYTHON) -m tools.rv32_rtl $(RV32_OS_MENU_ARGS) --backend emulator --emulator $(RV32EMU) --out build/rv32/os/menu-emu
run-rv32-os-menu-rtl-verilator: check-rv32-os-image $(RV32EMU) $(RV32_TB_VERILATOR)
	$(PYTHON) -m tools.rv32_rtl $(RV32_OS_MENU_ARGS) --emulator $(RV32EMU) --simulator $(RV32_TB_VERILATOR) --seed 17 --gpu-seed 31 --simd-seed 43 --max-cycles 200000000 --out build/rv32/os/menu-verilator
# O4: two programs share the machine, preempted by the timer. Each writes its result to a file the
# shell prints after `wait`, so the transcript does not depend on the interleaving; with cycle ticks
# only the number of frames is compared, in step-tick mode the whole trace.
RV32_OS_JOBS_ARGS := --image build/rv32/os/kernel.bin --console-input $(RV32_OS)/jobs.session --disk $(RV32_OS_DISK) \
	--expect-console-file $(RV32_OS)/jobs.session.expected --timeout 900
.PHONY: run-rv32-os-jobs-qemu run-rv32-os-jobs-emu run-rv32-os-jobs-rtl-verilator run-rv32-os-jobs-rtl-steps
run-rv32-os-jobs-qemu: check-rv32-os-image
	cp $(RV32_OS_DISK) build/rv32/os/jobs.qemu.disk
	$(PYTHON) tools/rv32_run_qemu.py build/rv32/os/kernel.elf --qemu $(QEMU_RV32) --cpu $(RV32_PLATFORM_QEMU_CPU) --icount $(RV32_OS_QEMU_ICOUNT) --stdin $(RV32_OS)/jobs.session --drive build/rv32/os/jobs.qemu.disk --last-line --timeout 60 --transcript build/rv32/os/jobs.qemu.transcript
	diff -u $(RV32_OS)/jobs.session.qemu.expected build/rv32/os/jobs.qemu.transcript
run-rv32-os-jobs-emu: check-rv32-os-image $(RV32EMU)
	$(PYTHON) -m tools.rv32_rtl $(RV32_OS_JOBS_ARGS) --compare results --backend emulator --emulator $(RV32EMU) --out build/rv32/os/jobs-emu
run-rv32-os-jobs-rtl-verilator: check-rv32-os-image $(RV32EMU) $(RV32_TB_VERILATOR)
	$(PYTHON) -m tools.rv32_rtl $(RV32_OS_JOBS_ARGS) --compare results --compare-traps faults --compare-checkpoints count --emulator $(RV32EMU) --simulator $(RV32_TB_VERILATOR) --stall 1 --max-cycles 100000000 --out build/rv32/os/jobs-verilator
run-rv32-os-jobs-rtl-steps: check-rv32-os-image $(RV32EMU) $(RV32_TB_VERILATOR)
	$(PYTHON) -m tools.rv32_rtl $(RV32_OS_JOBS_ARGS) --ticks steps --allow-traps --emulator $(RV32EMU) --simulator $(RV32_TB_VERILATOR) --seed 21 --max-cycles 100000000 --out build/rv32/os/jobs-steps
test-rv32: run-rv32-os-jobs-qemu run-rv32-os-jobs-emu run-rv32-os-jobs-rtl-verilator run-rv32-os-jobs-rtl-steps
test-rv32-os: check-rv32-os-image $(RV32EMU) $(RV32_TB_VERILATOR)
	HOST_CC=$(HOST_CC) RV32_RTL_SIM=$(RV32_TB_VERILATOR) QEMU_RV32=$(QEMU_RV32) $(PYTHON) -m unittest discover -s tests -p 'test_rv32_os.py' -v
test-rv32: run-rv32-os-qemu run-rv32-os-qemu-reboot run-rv32-os-emu run-rv32-os-rtl-verilator run-rv32-os-pong-emu run-rv32-os-pong-rtl-steps run-rv32-os-boot2 run-rv32-os-menu-emu run-rv32-os-menu-rtl-verilator test-rv32-os

# O3: storage. virtiocheck drives the virtio-blk device directly on QEMU virt (a 128 KiB drive on
# virtio-mmio-bus.0), the emulator (--disk) and the RTL (+disk); the runner compares the disks the
# backends leave. The kernel's file system (tfs) comes from tools/rv32_mkfs.py.
.PHONY: check-rv32-virtiocheck-image run-rv32-virtio-qemu run-rv32-virtio-emu run-rv32-virtio-rtl run-rv32-virtio-rtl-verilator run-rv32-virtio-rtl-steps
RV32_VIRTIOCHECK_OBJS := build/rv32/virtiocheck.o build/rv32/trap.o build/rv32/fdt.o $(RV32_COMMON_OBJS)
RV32_VIRTIOCHECK_HEX := e0cd1a7f
RV32_VIRTIO_ARGS := --image build/rv32/virtiocheck.bin --disk build/rv32/blank.disk --allow-traps --expect-last-line "PASS $(RV32_VIRTIOCHECK_HEX)" --expect-console-file programs/rv32/virtiocheck.expected
build/rv32/virtiocheck.o: programs/rv32/fdt.h programs/rv32/virtio_mmio.h
build/rv32/virtiocheck.elf: $(RV32_VIRTIOCHECK_OBJS) programs/rv32/link.ld
	$(RV32_CC) $(RV32_LDFLAGS) -Wl,-Map,$(@:.elf=.map) -o $@ $(RV32_VIRTIOCHECK_OBJS)
build/rv32/blank.disk: tools/rv32_mkfs.py | build/rv32
	$(PYTHON) tools/rv32_mkfs.py --new $@
check-rv32-virtiocheck-image: build/rv32/virtiocheck.elf build/rv32/virtiocheck.lst build/rv32/virtiocheck.bin build/rv32/blank.disk
	$(PYTHON) tools/rv32_image.py build/rv32/virtiocheck.elf --listing build/rv32/virtiocheck.lst --bin build/rv32/virtiocheck.bin --hex build/rv32/virtiocheck.hex --allow-privileged
run-rv32-virtio-qemu: check-rv32-virtiocheck-image
	cp build/rv32/blank.disk build/rv32/virtio.qemu.disk
	$(PYTHON) tools/rv32_run_qemu.py build/rv32/virtiocheck.elf --qemu $(QEMU_RV32) --cpu $(RV32_PLATFORM_QEMU_CPU) --drive build/rv32/virtio.qemu.disk --last-line --timeout 20 --expect-hex $(RV32_VIRTIOCHECK_HEX) --transcript build/rv32/virtiocheck.qemu.transcript
	diff -u programs/rv32/virtiocheck.qemu.expected build/rv32/virtiocheck.qemu.transcript
run-rv32-virtio-emu: check-rv32-virtiocheck-image $(RV32EMU)
	$(PYTHON) -m tools.rv32_rtl $(RV32_VIRTIO_ARGS) --backend emulator --emulator $(RV32EMU) --out build/rv32/virtio-emu
run-rv32-virtio-rtl: check-rv32-virtiocheck-image $(RV32EMU) $(RV32_TB_VVP)
	$(PYTHON) -m tools.rv32_rtl $(RV32_VIRTIO_ARGS) --emulator $(RV32EMU) --simulator $(RV32_TB_VVP) --rtl-timeout $(RV32_ICARUS_TIMEOUT) --out build/rv32/virtio-icarus
run-rv32-virtio-rtl-verilator: check-rv32-virtiocheck-image $(RV32EMU) $(RV32_TB_VERILATOR)
	$(PYTHON) -m tools.rv32_rtl $(RV32_VIRTIO_ARGS) --emulator $(RV32EMU) --simulator $(RV32_TB_VERILATOR) --seed 3 --gpu-seed 5 --out build/rv32/virtio-verilator
# The same check in step-tick mode with seeded stalls: the disk transfers keep the trace comparable.
run-rv32-virtio-rtl-steps: check-rv32-virtiocheck-image $(RV32EMU) $(RV32_TB_VERILATOR)
	$(PYTHON) -m tools.rv32_rtl $(RV32_VIRTIO_ARGS) --ticks steps --emulator $(RV32EMU) --simulator $(RV32_TB_VERILATOR) --seed 3 --out build/rv32/virtio-steps
test-rv32: run-rv32-virtio-qemu run-rv32-virtio-emu run-rv32-virtio-rtl run-rv32-virtio-rtl-verilator run-rv32-virtio-rtl-steps
