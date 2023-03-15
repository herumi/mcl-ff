#include <cybozu/test.hpp>
#include <mcl/bls12_381.hpp>
#include <cybozu/xorshift.hpp>
#include <cybozu/benchmark.hpp>
#include <mcl_fp.h>

using namespace mcl::bn;
using namespace mcl;

static const size_t N = 6;

static const int C = 100;
static const int CC = 10000;

CYBOZU_TEST_AUTO(init)
{
	initPairing(BLS12_381);
}

CYBOZU_TEST_AUTO(add)
{
	Unit xa[N], ya[N], za[N];
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
	}
	CYBOZU_BENCH_C("Fp::add", CC, Fp::add, z, x, y);
	CYBOZU_BENCH_C("fp_add ", CC, mcl_fp_add, za, xa, ya);
}

CYBOZU_TEST_AUTO(sub)
{
	Unit xa[N], ya[N], za[N];
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
	}
	CYBOZU_BENCH_C("Fp::sub", CC, Fp::sub, z, x, y);
	CYBOZU_BENCH_C("fp_sub ", CC, mcl_fp_sub, za, xa, ya);
}

CYBOZU_TEST_AUTO(mul)
{
	Unit xa[N], ya[N], za[N];
	Fp x, y, z;
	cybozu::XorShift rg;
	for (int i = 0; i < C; i++) {
		x.setByCSPRNG(rg);
		y.setByCSPRNG(rg);
		bint::copyN(xa, x.getUnit(), N);
		bint::copyN(ya, y.getUnit(), N);
		Fp::mul(z, x, y);
		mcl_fp_mont(za, xa, ya);
		CYBOZU_TEST_EQUAL_ARRAY(za, z.getUnit(), N);
	}
	CYBOZU_BENCH_C("Fp::mul", CC, Fp::mul, z, x, y);
	CYBOZU_BENCH_C("fp_mont", CC, mcl_fp_mont, za, xa, ya);
}
