PYTHON?=python3
CLANG?=clang++
ifeq ($(findstring $(CXX),clang),)
  CLANG=$(CXX)
endif
MCL_DIR?=../mcl

# register bit size
BIT?=64
# characteristic of a finite field
TYPE?=BLS12-381-p
ifeq ($(TYPE),BLS12-381-p)
  P=0x1a0111ea397fe69a4b1ba7b6434bacd764774b84f38512bf6730d2a0f6b0f6241eabfffeb153ffffb9feffffffffaaab
endif
ifeq ($(TYPE),BLS12-381-r)
  P=0x73eda753299d7d483339d80809a1d80553bda402fffe5bfeffffffff00000001
endif
# prefix of a function name
NAME?=mcl_fp
PRE?=$(NAME)_
LL=$(NAME).ll
HEADER=$(NAME).h

TEST_SRC=fp_test.cpp
TEST_EXE=$(TEST_SRC:.cpp=.exe)
DEPEND_FILE=$(TEST_SRC:.cpp=.d)

TARGET=$(LL) $(HEADER) $(TEST_EXE)

CFLAGS=-Wall -Wextra -I ./ -I $(MCL_DIR)/include -fPIC
LDFLAGS=$(NAME).o -lmcl -L $(MCL_DIR)/lib

ifeq ($(DEBUG),1)
else
  CFLAGS+=-O2 -DNDEBUG
endif

%.o: %.cpp
	$(CXX) -c -o $@ $< $(CFLAGS) -MMD -MP -MF $(@:.o=.d)
%.exe: %.o $(NAME).o $(HEADER)
	$(CXX) -o $@ $< $(LDFLAGS)

all: $(TARGET)

$(LL): gen_ff.py Makefile s_xbyak_llvm.py
	$(PYTHON) $< -u $(BIT) -p $(P) -pre $(PRE) > $@

$(NAME).o: $(LL)
	$(CLANG) -c -o $@ $< $(CFLAGS)

$(HEADER): gen_ff.py Makefile
	@cat header.h > $@
	@echo '// p=$(P)' >> $@
	@$(PYTHON) $< -u $(BIT) -proto >> $@
	@cat tail.h >> $@

test: $(TEST_EXE)
	@sh -ec 'for i in $(TEST_EXE); do echo $$i; env LSAN_OPTIONS=verbosity=0:log_threads=1 ./$$i; done'

x64asm: $(LL)
	$(CLANG) -o - -S -O2 $< -masm=intel -mbmi2

a64asm: $(LL)
	$(CLANG) -o - -S -O2 $< --target=aarch64

-include $(DEPEND_FILE)

.PHONY: clean

clean:
	rm -rf *.s *.o *.d $(LL) $(HEADER)

# don't remove these files automatically
.SECONDARY: $(TEST_SRC:.cpp=.o)
