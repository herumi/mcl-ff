#define CYBOZU_BENCH_USE_GETTIMEOFDAY
#include <cybozu/test.hpp>
#include <cybozu/xorshift.hpp>
#include <cybozu/benchmark.hpp>
#include <mcl_fp.h>
#include <mcl/fp_def.hpp>
#include <mcl/fp_tower.hpp>

using namespace mcl;
using namespace mcl::fp;

size_t N;

static const int C = 100;
static const int CC = 10000000;

static const size_t maxN = 8;

CYBOZU_TEST_AUTO(init)
{
	const char *pStr = (const char*)mcl_fp_get_prime();
	printf("p=%s\n", pStr);
	Fp::init(pStr, 1, 1);
	bool b;
	Fp2::init(&b);
	CYBOZU_TEST_ASSERT(b);
	N = Fp::getOp().N;
}

CYBOZU_TEST_AUTO(add)
{
	Fp x, y, z;
	Unit xa[maxN], ya[maxN], za[maxN];
	cybozu::XorShift rg;
	for (int i = 0; i < C; i++) {
		x.setByCSPRNG(rg);
		y.setByCSPRNG(rg);
		bint::copyN(xa, x.getUnit(), N);
		bint::copyN(ya, y.getUnit(), N);
		Fp::add(z, x, y);
		mcl_fp_add(za, xa, ya);
		CYBOZU_TEST_EQUAL_ARRAY(za, z.getUnit(), N);
	}
	CYBOZU_BENCH_C("fp_add", CC, mcl_fp_add, za, xa, ya);
}

CYBOZU_TEST_AUTO(sub)
{
	Fp x, y, z;
	Unit xa[maxN], ya[maxN], za[maxN];
	cybozu::XorShift rg;
	for (int i = 0; i < C; i++) {
		x.setByCSPRNG(rg);
		y.setByCSPRNG(rg);
		bint::copyN(xa, x.getUnit(), N);
		bint::copyN(ya, y.getUnit(), N);
		Fp::sub(z, x, y);
		mcl_fp_sub(za, xa, ya);
		CYBOZU_TEST_EQUAL_ARRAY(za, z.getUnit(), N);
	}
	CYBOZU_BENCH_C("fp_sub", CC, mcl_fp_sub, za, xa, ya);
}

CYBOZU_TEST_AUTO(mul)
{
	Fp x, y, z;
	Unit xa[maxN], ya[maxN], za[maxN];
	cybozu::XorShift rg;
	for (int i = 0; i < C; i++) {
		x.setByCSPRNG(rg);
		y.setByCSPRNG(rg);
		bint::copyN(xa, x.getUnit(), N);
		bint::copyN(ya, y.getUnit(), N);
		Fp::mul(z, x, y);
		mcl_fp_mul(za, xa, ya);
		CYBOZU_TEST_EQUAL_ARRAY(za, z.getUnit(), N);
#if 0
		if (!bint::cmpEqN(za, z.getUnit(), N)) {
			bint::dump(xa, N, "xa");
			bint::dump(ya, N, "ya");
			bint::dump(za, N, "za");
			bint::dump(z.getUnit(), N, "zb");
			exit(1);
		}
#endif
	}
	CYBOZU_BENCH_C("fp_mul", CC, mcl_fp_mul, za, xa, ya);
}

CYBOZU_TEST_AUTO(add2)
{
	const size_t M = sizeof(Fp) / sizeof(Unit);
	Fp2 x, y, z;
	Unit xa[maxN*2], ya[maxN*2], za[maxN*2];
	cybozu::XorShift rg;
	for (int i = 0; i < C; i++) {
		x.a.setByCSPRNG(rg);
		x.b.setByCSPRNG(rg);
		y.a.setByCSPRNG(rg);
		y.b.setByCSPRNG(rg);
		bint::copyN(xa, x.getUnit(), M*2);
		bint::copyN(ya, y.getUnit(), M*2);
		Fp2::add(z, x, y);
		mcl_fp2_add(za, xa, ya);
		CYBOZU_TEST_EQUAL_ARRAY(za, z.getUnit(), N);
		CYBOZU_TEST_EQUAL_ARRAY(za + M, z.getUnit() + M, N);
	}
	CYBOZU_BENCH_C("fp2_add", CC*10, mcl_fp2_add, za, xa, ya);
	CYBOZU_BENCH_C("Fp2::add", CC*10, Fp2::add, z, x, y);
}
