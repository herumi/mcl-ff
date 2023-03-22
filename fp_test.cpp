#define CYBOZU_BENCH_USE_GETTIMEOFDAY
#include <cybozu/test.hpp>
#include <cybozu/xorshift.hpp>
#include <cybozu/benchmark.hpp>
#include <mcl_fp.h>
#include <mcl/fp.hpp>
#ifdef MCL_FF_X64
extern "C" void mcl_fp_add_x64(uint64_t*, const uint64_t*, const uint64_t*);
extern "C" void mcl_fp_sub_x64(uint64_t*, const uint64_t*, const uint64_t*);
extern "C" void mcl_fp_mul_x64(uint64_t*, const uint64_t*, const uint64_t*);
#endif

typedef mcl::FpT<> Fp;
using namespace mcl;

size_t N;

static const int C = 100;
static const int CC = 1000000;

static const size_t maxN = 8;
Unit xa[maxN], ya[maxN], za[maxN];

CYBOZU_TEST_AUTO(init)
{
	const char *pStr = (const char*)mcl_fp_get_prime();
	printf("p=%s\n", pStr);
	Fp::init(pStr);
	N = Fp::getOp().N;
}

CYBOZU_TEST_AUTO(add)
{
	Fp x, y, z;
	cybozu::XorShift rg;
	for (int i = 0; i < C; i++) {
		x.setByCSPRNG(rg);
		y.setByCSPRNG(rg);
		bint::copyN(xa, x.getUnit(), N);
		bint::copyN(ya, y.getUnit(), N);
		Fp::add(z, x, y);
		mcl_fp_add(za, xa, ya);
		CYBOZU_TEST_EQUAL_ARRAY(za, z.getUnit(), N);
#ifdef MCL_FF_X64
		memset(za, 0, sizeof(xa));
		mcl_fp_add_x64(za, xa, ya);
		CYBOZU_TEST_EQUAL_ARRAY(za, z.getUnit(), N);
#endif
	}
	CYBOZU_BENCH_C("Fp::add", CC, Fp::add, z, x, y);
	CYBOZU_BENCH_C("fp_add", CC, mcl_fp_add, za, xa, ya);
#ifdef MCL_FF_X64
	CYBOZU_BENCH_C("fp_add_x64", CC, mcl_fp_add_x64, za, xa, ya);
#endif
}

CYBOZU_TEST_AUTO(sub)
{
	Fp x, y, z;
	cybozu::XorShift rg;
	for (int i = 0; i < C; i++) {
		x.setByCSPRNG(rg);
		y.setByCSPRNG(rg);
		bint::copyN(xa, x.getUnit(), N);
		bint::copyN(ya, y.getUnit(), N);
		Fp::sub(z, x, y);
		mcl_fp_sub(za, xa, ya);
		CYBOZU_TEST_EQUAL_ARRAY(za, z.getUnit(), N);
#ifdef MCL_FF_X64
		memset(za, 0, sizeof(xa));
		mcl_fp_sub_x64(za, xa, ya);
		CYBOZU_TEST_EQUAL_ARRAY(za, z.getUnit(), N);
#endif
	}
	CYBOZU_BENCH_C("Fp::sub", CC, Fp::sub, z, x, y);
	CYBOZU_BENCH_C("fp_sub", CC, mcl_fp_sub, za, xa, ya);
#ifdef MCL_FF_X64
	CYBOZU_BENCH_C("fp_sub_x64", CC, mcl_fp_sub_x64, za, xa, ya);
#endif
}

CYBOZU_TEST_AUTO(mul)
{
	Fp x, y, z;
	cybozu::XorShift rg;
	for (int i = 0; i < C; i++) {
		x.setByCSPRNG(rg);
		y.setByCSPRNG(rg);
		bint::copyN(xa, x.getUnit(), N);
		bint::copyN(ya, y.getUnit(), N);
		Fp::mul(z, x, y);
		mcl_fp_mul(za, xa, ya);
		CYBOZU_TEST_EQUAL_ARRAY(za, z.getUnit(), N);
#ifdef MCL_FF_X64
		memset(za, 0, sizeof(xa));
		mcl_fp_mul_x64(za, xa, ya);
		CYBOZU_TEST_EQUAL_ARRAY(za, z.getUnit(), N);
#endif
	}
	CYBOZU_BENCH_C("Fp::mul", CC, Fp::mul, z, x, y);
	CYBOZU_BENCH_C("fp_mul", CC, mcl_fp_mul, za, xa, ya);
#ifdef MCL_FF_X64
	CYBOZU_BENCH_C("fp_mul_x64", CC, mcl_fp_mul_x64, za, xa, ya);
#endif
}
