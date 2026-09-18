.DEFAULT_GOAL := test
PYTHON ?= python3

RTL := labs/01-counter/counter.v
TB := labs/01-counter/counter_tb.sv
ALU_RTL := labs/02-alu/alu.v
ALU_TB := labs/02-alu/alu_tb.sv
SAP8_RTL := rtl/sap8/sap8.v $(ALU_RTL)
SAP8_TB := tests/sap8_tb.sv
SAP8_ADD_ARGS := +program=build/sap8/add.program.hex +data=build/sap8/add.data.hex +expected=12 +instructions=6 +memory-address=240 +memory-value=7
SAP8_LOOP_ARGS := +program=build/sap8/sum_loop.program.hex +data=build/sap8/sum_loop.data.hex +expected=6 +instructions=29 +memory-address=241 +memory-value=0

.PHONY: test sim lint synth test-verilator waves clean
.PHONY: test-alu sim-alu lint-alu synth-alu test-alu-verilator waves-alu
.PHONY: test-sap8 sim-sap8 lint-sap8 synth-sap8 test-sap8-verilator waves-sap8
.PHONY: test-sap8-assembler programs-sap8

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

clean:
	rm -rf build
