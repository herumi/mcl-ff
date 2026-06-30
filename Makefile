PYTHON?=python3
CLANG?=clang++-20
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

TEST_SRC=test/fp_test.cpp
TEST_BASE=$(notdir $(TEST_SRC:.cpp=))
TEST_OBJ=obj/$(TEST_BASE).o
TEST_EXE=bin/$(TEST_BASE).exe
DEPEND_FILE=$(TEST_OBJ:.o=.d)

TARGET=$(LL) $(HEADER) $(TEST_EXE)

CFLAGS=-Wall -Wextra -I ./include -I $(MCL_DIR)/include -fPIC -g
LDFLAGS=$(MCL_FF_OBJ) $(MCL_LIB)

ifeq ($(ARCH),x86_64)
  GEN_OPT=-add -sub
  CFLAGS+=-mbmi2
else
  GEN_OPT=-add -sub -mul
endif

ifeq ($(ARCH),x86_64)
$(X64_ASM): src/gen_ff_x64.py
	$(PYTHON) $< -m gas > $@ -type $(TYPE) -mul
obj/$(NAME)_x64.o: $(X64_ASM)
	$(CXX) -c -o $@ $< -fPIC
MCL_FF_OBJ+=obj/$(NAME)_x64.o
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

$(LL): src/gen_ff.py Makefile src/s_xbyak_llvm.py
	$(PYTHON) $< -u $(BIT) -type $(TYPE) -pre $(PRE) $(GEN_OPT) > $@

obj/$(NAME).o: $(LL)
	$(CLANG) -c -o $@ $< $(CFLAGS)

$(HEADER): src/gen_ff.py Makefile
	@cat src/header.h > $@
	@echo '// p=$(P)' >> $@
	@$(PYTHON) $< -u $(BIT) -proto >> $@
	@cat src/tail.h >> $@

test: $(TEST_EXE)
	@sh -ec 'for i in $(TEST_EXE); do echo $$i; env LSAN_OPTIONS=verbosity=0:log_threads=1 ./$$i; done'

# Generate add/sub/mul from gen_ff.py (LLVM) and gen_ff_x64.py (x64 asm) under
# distinct prefixes and compare them within a single executable (test/bench.cpp).
BENCH_EXE=bin/bench.exe
src/bench_llvm.ll: src/gen_ff.py
	$(PYTHON) src/gen_ff.py -u 64 -type $(TYPE) -pre llvm_ -add -sub -mul > $@
src/bench_llvm_var.ll: src/gen_ff.py
	$(PYTHON) src/gen_ff.py -u 64 -type $(TYPE) -pre llvm_var_ -add -sub -mul -var-p > $@
src/bench_llvm_argp.ll: src/gen_ff.py
	$(PYTHON) src/gen_ff.py -u 64 -type $(TYPE) -pre llvm_argp_ -add -sub -mul -arg-p > $@
src/bench_x64.S: src/gen_ff_x64.py
	$(PYTHON) src/gen_ff_x64.py -m gas -type $(TYPE) -pre x64_ -add -sub -mul > $@
obj/bench_llvm.o: src/bench_llvm.ll
	$(CLANG) -c -o $@ $< $(CFLAGS) -mllvm -mul-constant-optimization=false
obj/bench_llvm_var.o: src/bench_llvm_var.ll
	$(CLANG) -c -o $@ $< $(CFLAGS)
obj/bench_llvm_argp.o: src/bench_llvm_argp.ll
	$(CLANG) -c -o $@ $< $(CFLAGS)
obj/bench_x64.o: src/bench_x64.S
	$(CXX) -c -o $@ $< -fPIC
$(BENCH_EXE): test/bench.cpp obj/bench_llvm.o obj/bench_llvm_var.o obj/bench_llvm_argp.o obj/bench_x64.o $(HEADER)
	$(CXX) -o $@ $< obj/bench_llvm.o obj/bench_llvm_var.o obj/bench_llvm_argp.o obj/bench_x64.o $(CFLAGS) $(MCL_LIB)
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

# don't remove these files automatically
.SECONDARY: $(TEST_OBJ)
