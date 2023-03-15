PYTHON?=python3
CLANG?=clang++

# register bit size
BIT?=64
# characteristic of a finite field
P?=0x1a0111ea397fe69a4b1ba7b6434bacd764774b84f38512bf6730d2a0f6b0f6241eabfffeb153ffffb9feffffffffaaab
# prefix of a function name
NAME?=mcl_fp
PRE?=$(NAME)_
LL=$(NAME).ll
HEADER=$(NAME).h

TARGET=$(LL) $(HEADER)

all: $(TARGET)

$(LL): gen_ff.py Makefile
	$(PYTHON) $< -u $(BIT) -p $(P) -pre $(PRE) > $@

$(HEADER): gen_ff.py Makefile
	@cat header.h > $@
	@echo '// p=$(P)' >> $@
	@$(PYTHON) $< -u $(BIT) -proto >> $@

asm: $(LL)
	$(CLANG) -S -O2 $< -masm=intel -mbmi2 -o -

.PHONY: clean

clean:
	rm -rf *.s *.o $(LL) $(HEADER)
