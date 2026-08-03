PYTHON?=python3
CLANG?=clang++
# use CXX as the LLVM compiler only when it is a clang variant
ifneq ($(findstring clang,$(CXX)),)
  CLANG=$(CXX)
endif
MCL_DIR?=../mcl
MCL_LIB=-lmcl -L $(MCL_DIR)/lib
ARCH?=$(shell uname -m)

.DEFAULT_GOAL := all

# register bit size
BIT?=64
# characteristic of a finite field
TYPE?=BLS12-381-p
# prefix of a function name
NAME?=mcl_fp
PRE?=$(NAME)_
LL=src/$(NAME).ll
HEADER=include/$(NAME).h
X64_ASM=src/$(NAME)_x64.S
MCL_FF_OBJ=obj/$(NAME).o

BENCH_SRC=test/bench.cpp
BENCH_BASE=$(notdir $(BENCH_SRC:.cpp=))
BENCH_OBJ=obj/$(BENCH_BASE).o
BENCH_EXE=bin/$(BENCH_BASE).exe
DEPEND_FILE=$(BENCH_OBJ:.o=.d)

TARGET=$(LL) $(HEADER) $(BENCH_EXE)

# Regenerate generated files when TYPE/BIT/NAME change: GEN_STAMP is rewritten
# (updating its mtime) only when its content differs from the current values,
# so switching TYPE rebuilds exactly the targets that depend on it.
GEN_STAMP=obj/.gen_param
.PHONY: FORCE
FORCE:
$(GEN_STAMP): FORCE
	@echo '$(TYPE) $(BIT) $(NAME)' | cmp -s - $@ || echo '$(TYPE) $(BIT) $(NAME)' > $@

CFLAGS=-std=c++17 -Wall -Wextra -I ./include -I $(MCL_DIR)/include -fPIC -g
#CFLAGS+=-Wno-unused-command-line-argument -Wno-override-module
LDFLAGS=$(MCL_FF_OBJ) $(MCL_LIB)

ifeq ($(ARCH),x86_64)
GEN_OPT=-add -sub
CFLAGS+=-mbmi2
BENCH_X64_OBJ=obj/bench_x64.o
else
# and-mask sub reduction: faster than the {0,p} table on aarch64
SUB_OPT=-sub_mask
GEN_OPT=-add -sub -mul $(SUB_OPT)
endif

ifeq ($(ARCH),x86_64)
$(X64_ASM): src/gen_ff_x64.py $(GEN_STAMP)
	$(PYTHON) $< -m gas > $@ -type $(TYPE) -mul
obj/$(NAME)_x64.o: $(X64_ASM)
	$(CXX) -c -o $@ $< -fPIC
MCL_FF_OBJ+=obj/$(NAME)_x64.o
BENCH_X64_OBJ=obj/bench_x64.o
endif

# On Mach-O an undefined weak symbol is a link error (unlike ELF, where it
# resolves to NULL); -U lets llvm2_sqr stay undefined so that types failing
# the nocarry condition (e.g. BLS12-381-r) still link (bench.cpp skips it).
# Likewise llvm_mod128 (generated only for even N and non-full-bit p).
ifeq ($(shell uname -s),Darwin)
BENCH_LDFLAGS=-Wl,-U,_llvm2_sqr -Wl,-U,_llvm_mod128 -Wl,-U,_llvm_mul128
endif

ifeq ($(DEBUG),1)
else
  CFLAGS+=-O2 -DNDEBUG
endif

obj/%.o: test/%.cpp
	$(CXX) -c -o $@ $< $(CFLAGS) -MMD -MP -MF $(@:.o=.d)
bin/%.exe: obj/%.o $(MCL_FF_OBJ) $(HEADER)
	$(CXX) -o $@ $< $(LDFLAGS)

all: $(TARGET)

$(LL): src/gen_ff.py Makefile src/s_xbyak_llvm.py $(GEN_STAMP)
	$(PYTHON) $< -u $(BIT) -type $(TYPE) -pre $(PRE) $(GEN_OPT) > $@

obj/$(NAME).o: $(LL)
	$(CLANG) -c -o $@ $< $(CFLAGS)

$(HEADER): src/gen_ff.py Makefile $(GEN_STAMP)
	@cat src/header.h > $@
	@echo '// p=$(P)' >> $@
	@$(PYTHON) $< -u $(BIT) -proto >> $@
	@cat src/tail.h >> $@

test: $(BENCH_EXE)
	$(BENCH_EXE) -mode 1
#	@sh -ec 'for i in $(TEST_EXE); do echo $$i; env LSAN_OPTIONS=verbosity=0:log_threads=1 ./$$i; done'

# Generate add/sub/mul from gen_ff.py (LLVM) and, on x86_64, gen_ff_x64.py
# (x64 asm) under distinct prefixes and compare them within a single executable
# (test/bench.cpp).
src/bench_llvm.ll: src/gen_ff.py $(GEN_STAMP)
	$(PYTHON) src/gen_ff.py -u 64 -type $(TYPE) -pre llvm_ -add -sub -mul -mul128 -sqr -mod -mod128 -mulPre -sqrPre -fp2_mul -fp2_sqr $(SUB_OPT) > $@
obj/bench_llvm.o: src/bench_llvm.ll
	$(CLANG) -c -o $@ $< $(CFLAGS) -mllvm -mul-constant-optimization=false
ifeq ($(ARCH),x86_64)
src/bench_x64.S: src/gen_ff_x64.py $(GEN_STAMP)
	$(PYTHON) src/gen_ff_x64.py -m gas -type $(TYPE) -pre x64_ -add -sub -mul -mul_wo_adx -sqr -mulPre -mulPre_wo_adx -mod -mod128 -sqrPre -fp2_mul -fp2_sqr > $@
$(BENCH_X64_OBJ): src/bench_x64.S
	$(CXX) -c -o $@ $< -fPIC
endif
$(BENCH_EXE): test/bench.cpp obj/bench_llvm.o $(BENCH_X64_OBJ) $(HEADER)
	$(CXX) -o $@ $< obj/bench_llvm.o $(BENCH_X64_OBJ) $(CFLAGS) $(MCL_LIB) $(BENCH_LDFLAGS)
bench: $(BENCH_EXE)
	$(BENCH_EXE)

# secp256k1-p/r are excluded because they do not support non-montgomery
TYPE_TBL=BLS12-381-p BLS12-381-r BN254-p BN254-r

test_all:
	@for t in $(TYPE_TBL); do \
		echo $$t ; $(MAKE) clean test TYPE=$$t || exit 1; \
	done

bench_all:
	@for t in $(TYPE_TBL); do \
		echo $$t ; $(MAKE) clean bench TYPE=$$t || exit 1; \
	done

x64asm: $(LL)
	$(CLANG) -o - -S -O2 $< -masm=intel -mbmi2

a64asm: $(LL)
	$(CLANG) -o - -S -O2 $< --target=aarch64

-include $(DEPEND_FILE)

.PHONY: clean bench

clean:
	rm -rf src/*.s src/*.S src/*.ll obj/*.o obj/*.d $(HEADER) bin/*.exe
