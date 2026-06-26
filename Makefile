PYTHON?=python3
CLANG?=clang++
ifeq ($(findstring $(CXX),clang),)
  CLANG=$(CXX)
endif
MCL_DIR?=../mcl
ARCH?=$(shell uname -m)

# register bit size
BIT?=64
# characteristic of a finite field
TYPE?=BLS12-381-p
# prefix of a function name
NAME?=mcl_fp
PRE?=$(NAME)_
LL=$(NAME).ll
HEADER=$(NAME).h
MCL_FF_OBJ=$(NAME).o

TEST_SRC=fp_test.cpp
TEST_EXE=$(TEST_SRC:.cpp=.exe)
DEPEND_FILE=$(TEST_SRC:.cpp=.d)

TARGET=$(LL) $(HEADER) $(TEST_EXE)

CFLAGS=-Wall -Wextra -I ./ -I $(MCL_DIR)/include -fPIC
LDFLAGS=$(MCL_FF_OBJ) -lmcl -L $(MCL_DIR)/lib

ifeq ($(ARCH),x86_64)
  GEN_OPT=-add -sub
  CFLAGS+=-mbmi2
else
  GEN_OPT=-add -sub -mul
endif

ifeq ($(ARCH),x86_64)
$(NAME)_x64.S: gen_ff_x64.py
	$(PYTHON) $< -m gas > $@ -type $(TYPE) -mul
$(NAME)_x64.o: $(NAME)_x64.S
	$(CXX) -c -o $@ $< -fPIC
MCL_FF_OBJ+=$(NAME)_x64.o
endif

ifeq ($(DEBUG),1)
else
  CFLAGS+=-O2 -DNDEBUG
endif

%.o: %.cpp
	$(CXX) -c -o $@ $< $(CFLAGS) -MMD -MP -MF $(@:.o=.d)
%.exe: %.o $(MCL_FF_OBJ) $(HEADER)
	$(CXX) -o $@ $< $(LDFLAGS)

all: $(TARGET)

$(LL): gen_ff.py Makefile s_xbyak_llvm.py
	$(PYTHON) $< -u $(BIT) -type $(TYPE) -pre $(PRE) $(GEN_OPT) > $@

$(NAME).o: $(LL)
	$(CLANG) -c -o $@ $< $(CFLAGS)

$(HEADER): gen_ff.py Makefile
	@cat header.h > $@
	@echo '// p=$(P)' >> $@
	@$(PYTHON) $< -u $(BIT) -proto >> $@
	@cat tail.h >> $@

test: $(TEST_EXE)
	@sh -ec 'for i in $(TEST_EXE); do echo $$i; env LSAN_OPTIONS=verbosity=0:log_threads=1 ./$$i; done'

# Generate add/sub/mul from gen_ff.py (LLVM) and gen_ff_x64.py (x64 asm) under
# distinct prefixes and compare them within a single executable (misc/bench.cpp).
BENCH_EXE=bench.exe
bench_llvm.ll: gen_ff.py
	$(PYTHON) gen_ff.py -u 64 -type $(TYPE) -pre llvm_ -add -sub -mul > $@
bench_x64.S: gen_ff_x64.py
	$(PYTHON) gen_ff_x64.py -m gas -type $(TYPE) -pre x64_ -add -sub -mul > $@
bench_llvm.o: bench_llvm.ll
	$(CLANG) -c -o $@ $< $(CFLAGS) -mllvm -mul-constant-optimization=false
bench_x64.o: bench_x64.S
	$(CXX) -c -o $@ $< -fPIC
$(BENCH_EXE): misc/bench.cpp bench_llvm.o bench_x64.o
	$(CXX) -o $@ $< bench_llvm.o bench_x64.o $(CFLAGS)
bench: $(BENCH_EXE)
	./$(BENCH_EXE)

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
	rm -rf *.s *.S *.o *.d *.ll $(HEADER) *.exe

# don't remove these files automatically
.SECONDARY: $(TEST_SRC:.cpp=.o)
