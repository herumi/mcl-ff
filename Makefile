PYTHON?=python3
BIT?=64
CLANG?=clang++

all=mcl_ff.h mcl_ff.ll

mcl_ff.ll: gen_ff.py
	$(PYTHON) $< -u $(BIT) > $@

asm: mcl_ff.ll
	$(CLANG) -S -O2 $< -masm=intel
	cat mcl_ff.s

clean:
	rm -rf *.ll *.s *.o
