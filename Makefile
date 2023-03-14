PYTHON?=python3
CLANG?=clang++

# register bit size
BIT?=64
# characteristic of a finite field
P?=0x1a0111ea397fe69a4b1ba7b6434bacd764774b84f38512bf6730d2a0f6b0f6241eabfffeb153ffffb9feffffffffaaab
# prefix of a function name
PRE?=mclb_fp_

TARGET=mcl_ff.ll mcl_ff.h

all: $(TARGET)

mcl_ff.ll: gen_ff.py
	$(PYTHON) $< -u $(BIT) -p $(P) -pre $(PRE) > $@

mcl_ff.h: gen_ff.py Makefile
	@cat header.h > $@
	@echo '// p=$(P)' >> $@
	@$(PYTHON) $< -u $(BIT) -proto >> $@

asm: mcl_ff.ll
	$(CLANG) -S -O2 $< -masm=intel -mbmi2
	cat mcl_ff.s

.PHONY: clean mcl_ff.h

clean:
	rm -rf *.ll *.s *.o mcl_ff.h
