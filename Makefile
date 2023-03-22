PYTHON?=python3
CLANG?=clang++
ifeq ($(findstring $(CXX),clang),)
  CLANG=$(CXX)
endif
MCL_DIR?=../mcl
ARCH?=$(shell uname -m)
ifeq ($(ARCH),x86_64)
  X64_ASM=1
  GEN_OPT=-x64
endif

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

ifeq ($(X64_ASM),1)
$(NAME)_x64.S: gen_ff_x64.py
	$(PYTHON) $< -m gas > $@ -type $(TYPE)
$(NAME)_x64.o: $(NAME)_x64.S
	$(CXX) -c -o $@ $< -fPIC
CFLAGS+=-DMCL_FF_X64
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

test_all:
	$(MAKE) clean test TYPE=BLS12-381-p
	$(MAKE) clean test TYPE=BLS12-381-r
	$(MAKE) clean test TYPE=BN254-p
	$(MAKE) clean test TYPE=BN254-r

x64asm: $(LL)
	$(CLANG) -o - -S -O2 $< -masm=intel -mbmi2

a64asm: $(LL)
	$(CLANG) -o - -S -O2 $< --target=aarch64

-include $(DEPEND_FILE)

.PHONY: clean

clean:
	rm -rf *.s *.S *.o *.d $(LL) $(HEADER)

# don't remove these files automatically
.SECONDARY: $(TEST_SRC:.cpp=.o)
