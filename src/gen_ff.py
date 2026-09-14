import os
import sys
import argparse
# s_xbyak_llvm.py (the LLVM-IR DSL) and common.py (helpers shared with gen.py /
# gen_bint.py: gen_mulUU / gen_mulPos / gen_mulPv / emit_mulPre / emit_fp_add /
# emit_fp_sub_raw / emit_mont / emit_montRed / split) live in mcl: $MCL_DIR/src,
# default ../mcl relative to this repository. This repository has no copy of
# the DSL (removed 2026-09-14).
mclDir = os.environ.get('MCL_DIR', os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..', 'mcl'))
sys.path.append(os.path.join(mclDir, 'src'))
from s_xbyak_llvm import *
from mont import *
from primetbl import *
import common

unit = 0
unit2 = 0
mont = None

def gen_fp_add(name, N, dataVar):
  bit = unit * N
  resetGlobalIdx();
  pz = IntPtr(unit)
  px = IntPtr(unit)
  py = IntPtr(unit)
  with Function(name, Void, pz, px, py):
    pp = bitcast(dataVar, unit)
    # volatile: keep the operand loads unfused so store-forwarded inputs
    # (common in dependency chains) do not pay the folded-load latency.
    x = loadN(px, N, volatile=True)
    y = loadN(py, N, volatile=True)
    p = loadN(pp, N)
    x = common.emit_fp_add(unit, x, y, p, mont.isFullBit)
    storeN(x, pz)
    ret(Void)

def gen_fp2_add(name, N, dataVar, offset):
  bit = unit * N
  resetGlobalIdx();
  pz = IntPtr(unit)
  px = IntPtr(unit)
  py = IntPtr(unit)
  with Function(name, Void, pz, px, py):
    pp = bitcast(dataVar, unit)
    p = loadN(pp, N)
    for i in range(2):
      x = loadN(px, N, offset=i*offset, volatile=True)
      y = loadN(py, N, offset=i*offset, volatile=True)
      x = common.emit_fp_add(unit, x, y, p, mont.isFullBit)
      storeN(x, pz, offset=i*offset)

    ret(Void)

# Writable {zero, p} table for the sub reduction. Layout is
# [Npad x i64] zero, then p, padded to 2*Npad limbs (Npad = N rounded up to a
# power of two so the borrow-scaled offset is a single shift and each entry is
# cache-line aligned). It must be a non-constant global with external linkage:
# if the optimizer can prove the contents (constant, or internal + never
# stored), it folds the conditional +p back into an and-mask/cmov sequence.
def makeSubTbl(pre, mont):
  N = mont.pn
  Npad = 1 << (N - 1).bit_length()
  mask = (1 << unit) - 1
  limbs = [(mont.p >> (unit * i)) & mask for i in range(N)]
  v = [0] * Npad + limbs + [0] * (Npad - N)
  tbl = makeVar(f'{pre}sub_tbl', unit, v, static=False, const=False, align=64)
  return (tbl, Npad)

# Reduction via the {zero, p} table indexed by the borrow. The variable-index
# GEP cannot be rewritten into a select of the loaded values (the table is
# writable memory), so the conditional +p lowers to an add/adc chain with
# folded memory operands: the same idiom as the hand-written x64 asm.
def gen_sub_raw_tbl(x, y, ptbl, Npad, isFullBit):
  bit = x.bit
  v, c = common.emit_fp_sub_raw(unit, x, y, isFullBit)
  off = shl(zext(c, unit), Npad.bit_length() - 1)
  addr = getelementptr(ptbl, off)
  p = load(bitcast(addr, bit))
  v = add(v, p)
  return v

# Reduction via an and-mask: p is loaded from a fixed address known at
# function entry, so the load runs in parallel with the subtraction and only
# sext -> and -> add follow the borrow. The table variant instead derives the
# load address from the borrow, which puts the L1 load-use latency (~4 cycles)
# on the dependency chain when the borrow pattern defeats address prediction;
# on aarch64 this made sub latency 1.23x of mcl. On x64 the table still wins
# because it lowers to add/adc with folded memory operands, so this variant is
# selected by -sub_mask (passed by the Makefile on non-x86_64).
def gen_sub_raw_mask(x, y, p, isFullBit):
  bit = x.bit
  v, c = common.emit_fp_sub_raw(unit, x, y, isFullBit)
  v = add(v, and_(p, sext(c, bit)))
  return v

def gen_fp_sub(name, N, subTbl, dataVar, useMask):
  bit = unit * N
  resetGlobalIdx();
  pz = IntPtr(unit)
  px = IntPtr(unit)
  py = IntPtr(unit)
  with Function(name, Void, pz, px, py):
    if useMask:
      p = loadN(bitcast(dataVar, unit), N)
    else:
      tbl, Npad = subTbl
      ptbl = bitcast(tbl, unit)
    x = loadN(px, N, volatile=True)
    y = loadN(py, N, volatile=True)
    if useMask:
      v = gen_sub_raw_mask(x, y, p, mont.isFullBit)
    else:
      v = gen_sub_raw_tbl(x, y, ptbl, Npad, mont.isFullBit)
    storeN(v, pz)
    ret(Void)

def gen_fp2_sub(name, N, subTbl, dataVar, useMask, offset):
  bit = unit * N
  resetGlobalIdx();
  pz = IntPtr(unit)
  px = IntPtr(unit)
  py = IntPtr(unit)
  with Function(name, Void, pz, px, py):
    if useMask:
      p = loadN(bitcast(dataVar, unit), N)
    else:
      tbl, Npad = subTbl
      ptbl = bitcast(tbl, unit)
    for i in range(2):
      x = loadN(px, N, offset=i*offset, volatile=True)
      y = loadN(py, N, offset=i*offset, volatile=True)
      if useMask:
        v = gen_sub_raw_mask(x, y, p, mont.isFullBit)
      else:
        v = gen_sub_raw_tbl(x, y, ptbl, Npad, mont.isFullBit)
      storeN(v, pz, offset=i*offset)

    ret(Void)

# Fused Montgomery mul: z = x y R^-1 mod p; the body is common.emit_mont
# (shared with mcl_fp_mont of gen.py), with ip passed as an immediate.
def gen_mul(name, mont, dataVar, mulUnit):
  N = mont.pn
  resetGlobalIdx()
  pz = IntPtr(unit)
  px = IntPtr(unit)
  py = IntPtr(unit)
  with Function(name, Void, pz, px, py) as f:
    pp = bitcast(dataVar, unit)
    common.emit_mont(unit, N, pz, px, py, pp, mont.ip, mulUnit, mont.isFullBit)
    ret(Void)
  return f

# Montgomery reduction: z = xy R^-1 mod p where xy has 2N units; the body is
# common.emit_montRed (shared with mcl_fp_montRed of gen.py). The high units
# are fetched from memory via the getHi callback (gen_sqr passes an SSA value).
def gen_mod(name, mont, dataVar, mulUnit):
  N = mont.pn
  resetGlobalIdx()
  pz = IntPtr(unit)
  pxy = IntPtr(unit)
  with Function(name, Void, pz, pxy) as f:
    pp = bitcast(dataVar, unit)
    lo = loadN(pxy, N)
    p = loadN(pp, N)
    z = common.emit_montRed(unit, N, lo, lambda i: load(getelementptr(pxy, N + i)), pp, p, mont.ip, mulUnit, mont.isFullBit)
    storeN(z, pz)
    ret(Void)
  return f

# Radix-2^128 variant of common.emit_montRed. The serial recurrence of it
# (t0 -> q = t0*ip -> p[0]*q -> new t0, ~8-9 cycles/unit on Apple M4) is the
# bottleneck of the reduction, so q is computed two units at a time from the
# current t via ip2 = -p^-1 mod 2^128: the odd step's q no longer waits for
# the even step's accumulation and the recurrence has half as many stages.
# The cost is hi64((t mod 2^128) * ip2) (umulh + 2 madd per pair). The
# accumulation is kept in exactly the shape of emit_montRed: merging the two p*q
# rows before adding them to t makes clang interleave two carry chains via
# mrs/msr NZCV and costs 10% throughput. See memo.md 2026-08-03.
# Requires N even and p not full bit.
def mod128_raw(lo, getHi, mont, pp, mulUnit):
  N = mont.pn
  assert N % 2 == 0 and not mont.isFullBit
  bit = unit * N
  bu = bit + unit
  bu2 = bit + unit * 2
  u2 = unit * 2
  ip2 = (-pow(mont.p, -1, 1 << u2)) % (1 << u2)
  p = loadN(pp, N)
  t = lo
  H = None
  for i in range(N // 2):
    q = mul(trunc(t, u2), ip2)
    qs = [trunc(q, unit), trunc(lshr(q, unit), unit)]
    for j in range(2):
      pq = call(mulUnit, pp, qs[j])
      if i > 0 or j > 0:
        # previous carry, added into the top unit of pq (headroom: p is not
        # full bit)
        pq = add(pq, shl(zext(H, bu), bit))
      nxt = getHi(2 * i + j)
      t = pack([t, nxt])
      t = add(zext(t, bu2), zext(pq, bu2))
      t = lshr(t, unit)
      t = trunc(t, bu)
      H, t = common.split(t, bit)
  vc = sub(t, p)
  c = trunc(lshr(vc, bit - 1), 1)
  return select(c, t, vc)

# Montgomery reduction, radix-2^128 variant (see mod128_raw).
def gen_mod128(name, mont, dataVar, mulUnit):
  N = mont.pn
  resetGlobalIdx()
  pz = IntPtr(unit)
  pxy = IntPtr(unit)
  with Function(name, Void, pz, pxy) as f:
    pp = bitcast(dataVar, unit)
    lo = loadN(pxy, N)
    z = mod128_raw(lo, lambda i: load(getelementptr(pxy, N + i)), mont, pp, mulUnit)
    storeN(z, pz)
    ret(Void)
  return f

# Radix-2^128 variant of the fused Montgomery mul (see mod128_raw for the
# idea): q is computed two units at a time via ip2 = -p^-1 mod 2^128, so the
# serial recurrence t0 -> q -> p[0]*q -> new t0 has half as many stages.
# The row accumulation is kept in exactly the shape of gen_mul (one unit at a
# time); only the q supply changes. The pair's q needs t mod 2^128 after the
# x*y[2i] row plus the contribution of the x*y[2i+1] row to the second unit,
# which is just lo(x[0]*y[2i+1]): only that single unit is computed early, so
# the full xy1 row is not kept live across the first reduction row (computing
# it early doubles the live ranges and costs ~2x stack traffic on x64).
# Requires N even and p not full bit.
def gen_mul128(name, mont, dataVar, mulUnit):
  N = mont.pn
  assert N % 2 == 0 and not mont.isFullBit
  bit = unit * N
  u2 = unit * 2
  ip2 = (-pow(mont.p, -1, 1 << u2)) % (1 << u2)
  resetGlobalIdx()
  pz = IntPtr(unit)
  px = IntPtr(unit)
  py = IntPtr(unit)
  with Function(name, Void, pz, px, py) as f:
    pp = bitcast(dataVar, unit)
    x0 = load(px)
    for i in range(N // 2):
      y0 = load(getelementptr(py, 2 * i))
      xy0 = call(mulUnit, px, y0)
      if i == 0:
        t = xy0
      else:
        t = add(t, xy0)
      y1 = load(getelementptr(py, 2 * i + 1))
      m1 = mul(x0, y1) # low unit of the x*y[2i+1] row
      lo2 = add(trunc(t, u2), shl(zext(m1, u2), unit))
      q = mul(lo2, ip2)
      qs = [trunc(q, unit), trunc(lshr(q, unit), unit)]
      pq = call(mulUnit, pp, qs[0])
      t = add(t, pq)
      t = lshr(t, unit)
      xy1 = call(mulUnit, px, y1)
      t = add(t, xy1)
      pq = call(mulUnit, pp, qs[1])
      t = add(t, pq)
      t = lshr(t, unit)
    t = trunc(t, bit)
    vc = sub(t, loadN(pp, N))
    c = trunc(lshr(vc, bit - 1), 1)
    z = select(c, t, vc)
    storeN(z, pz)
    ret(Void)
  return f

# mulPre: pz[2N] = px[N] * py[N] (no reduction); the schoolbook body is
# common.emit_mulPre (shared with mclb_mul of gen_bint.py).
def gen_mulPre(name, N, mulUnit):
  resetGlobalIdx()
  pz = IntPtr(unit)
  px = IntPtr(unit)
  py = IntPtr(unit)
  with Function(name, Void, pz, px, py) as f:
    common.emit_mulPre(unit, N, pz, px, py, mulUnit)
    ret(Void)
  return f

# mulPreWide: pz[2N] = px[N] * py[N] via a single wide "mul i(2*bit)"
def gen_mulPreWide(name, N):
  resetGlobalIdx()
  pz = IntPtr(unit)
  px = IntPtr(unit)
  py = IntPtr(unit)
  bit = unit * N
  with Function(name, Void, pz, px, py) as f:
    x = zext(loadN(px, N), bit * 2)
    y = zext(loadN(py, N), bit * 2)
    storeN(mul(x, y), pz)
    ret(Void)
  return f

# sqrPreWide: pz[2N] = px[N]^2 via a single wide "mul i(2*bit)"
def gen_sqrPreWide(name, N):
  resetGlobalIdx()
  pz = IntPtr(unit)
  px = IntPtr(unit)
  bit = unit * N
  with Function(name, Void, pz, px) as f:
    x = zext(loadN(px, N), bit * 2)
    storeN(mul(x, x), pz)
    ret(Void)
  return f

# If True then sqrPre(z, x) is a call to mulPre(z, x, x), as in mcl's
# gen_mcl_fpDbl_sqrPre, instead of the dedicated schedule below.
# This used to be the fastest variant: the old row-major triangle accumulation
# rippled every add's carry up to the top of one wide accumulator and lost to
# the plain 36-mulx schoolbook. The bottom-up anti-diagonal schedule below
# matches the handwritten x64 sqrPre6, so the call variant is now obsolete.
USE_MULPRE_FOR_SQRPRE = not True

# sqrPre: pz[2N] = px[N]^2 (no reduction).
# Same schedule as the handwritten x64 sqrPre6 of fp_generator.hpp: the cross
# products on the anti-diagonal d = j - i, x[i]*x[i+d], sit at limbs
# d, d+2, ... and tile without overlap, so a row is a plain concat (pack).
# Rows are accumulated bottom-up (d = N-1 .. 1); each row extends the
# accumulator by one limb at both ends, so a row add is one short carry chain
# absorbed in the row's own top limb. Keeping the accumulator at its minimal
# width (grow by 2 limbs per row, no early zext to 2N limbs) matters: with
# full-width adds clang keeps 2N-limb values live and spills heavily.
# Finally double the accumulator (each cross term appears twice by symmetry)
# and add the diagonal squares x[i]^2, which tile the full 2N limbs exactly.
def sqrPre_raw(x, N):
  bit2 = unit * N * 2
  if N == 1:
    return mul(zext(x[0], unit2), zext(x[0], unit2))
  acc = None
  for d in range(N - 1, 0, -1):
    row = pack([mul(zext(x[i], unit2), zext(x[i + d], unit2)) for i in range(N - d)])
    if acc is None:
      acc = row
    else:
      acc = add(shl(zext(acc, row.bit), unit), row)
  acc = zext(acc, acc.bit + unit)
  acc = add(acc, acc)
  z = shl(zext(acc, bit2), unit)
  # emit the diagonal mulx last, close to their only use: hoisting them to
  # the top lengthens their live ranges and costs ~1 cycle in practice
  diag = pack([mul(zext(x[i], unit2), zext(x[i], unit2)) for i in range(N)])
  z = add(z, diag)
  return z

def gen_sqrPre(name, N, mulPreF):
  resetGlobalIdx()
  pz = IntPtr(unit)
  px = IntPtr(unit)
  with Function(name, Void, pz, px):
    if USE_MULPRE_FOR_SQRPRE:
      call(mulPreF, pz, px, px)
      ret(Void)
      return
    x = [load(getelementptr(px, i)) for i in range(N)]
    storeN(sqrPre_raw(x, N), pz)
    ret(Void)

# If True then sqr(z, x) is a call to mul(z, x, x) instead of the fused
# sqrPre + Montgomery reduction below. The fused variant needs fewer muls
# (N(N+1)/2 + N^2 + N = 63 vs 2N^2 + N = 78 for N=6) but loses to mul(x, x)
# on both Xeon w9-3495X (26.8/21.9 vs 23.7/19.9 ns latency/throughput,
# BLS12-381-p) and Apple M4 (21.5/14.8 vs 18.7/14.1): sqrPre_raw keeps the
# whole 2N-limb product live when the serial reduction starts, which the
# register file cannot hold, and the saved muls are eaten by spills.
# See memo.md 2026-07-27.
USE_MUL_FOR_SQR = True

# sqr: z = x^2 R^-1 mod p. A call to mul (see USE_MUL_FOR_SQR above), or the
# fused variant: the 2N-unit product of sqrPre_raw stays in one SSA value and
# is reduced in place by emit_montRed, so the intermediate never goes through
# memory and the call overhead of a sqrPre + mod pair is gone.
def gen_sqr(name, mont, dataVar, mulUnit, mulF):
  N = mont.pn
  bit = unit * N
  resetGlobalIdx()
  pz = IntPtr(unit)
  px = IntPtr(unit)
  with Function(name, Void, pz, px):
    if USE_MUL_FOR_SQR:
      call(mulF, pz, px, px)
      ret(Void)
      return
    pp = bitcast(dataVar, unit)
    x = [load(getelementptr(px, i)) for i in range(N)]
    xy = sqrPre_raw(x, N)
    lo = trunc(xy, bit)
    p = loadN(pp, N)
    z = common.emit_montRed(unit, N, lo, lambda i: trunc(lshr(xy, bit + i * unit), unit), pp, p, mont.ip, mulUnit, mont.isFullBit)
    storeN(z, pz)
    ret(Void)

# Fp2 mul: (z.a, z.b) = (a c - b d, a d + b c) where x = (a, b), y = (c, d),
# each component N limbs in Montgomery form, b at offset limbs from a.
# Same Karatsuba structure as gen_fp2_mul of gen_ff_x64.py:
#   s = a + b, t = c + d (no carry out since p is not full bit)
#   d1 = s t, d0 = a c, d2 = b d (3 mulPre calls on alloca buffers)
#   d1 -= d0; d1 -= d2 (= a d + b c; no borrow since s t >= a c + b d)
#   d0 -= d2 (mod p 2^bit: on borrow, add p to the high half; the +p comes
#     from the writable {zero, p} table like gen_sub_raw_tbl, so it lowers
#     to an add chain with memory operands instead of a 2N-limb select)
#   z.a = mod(d0), z.b = mod(d1)
def gen_fp2_mul(name, mont, mulPreF, modF, subTbl, offset):
  N = mont.pn
  bit = unit * N
  bit2 = bit * 2
  resetGlobalIdx()
  pz = IntPtr(unit)
  px = IntPtr(unit)
  py = IntPtr(unit)
  with Function(name, Void, pz, px, py):
    tbl, Npad = subTbl
    ptbl = bitcast(tbl, unit)
    ps = alloca_(unit, N)
    pt = alloca_(unit, N)
    pd0 = alloca_(unit, 2*N)
    pd1 = alloca_(unit, 2*N)
    pd2 = alloca_(unit, 2*N)
    a = loadN(px, N)
    b = loadN(px, N, offset=offset)
    c = loadN(py, N)
    d = loadN(py, N, offset=offset)
    storeN(add(a, b), ps)
    storeN(add(c, d), pt)
    call(mulPreF, pd1, ps, pt)
    call(mulPreF, pd0, px, py)
    call(mulPreF, pd2, getelementptr(px, offset), getelementptr(py, offset))
    d0 = loadN(pd0, 2*N)
    d1 = loadN(pd1, 2*N)
    d2 = loadN(pd2, 2*N)
    d1 = sub(sub(d1, d0), d2)
    storeN(d1, pd1)
    v = sub(d0, d2)
    # borrow flag: d0, d2 < p^2 < 2^(bit2-2), so the top bit is set iff
    # the sub wrapped around
    c = trunc(lshr(v, bit2 - 1), 1)
    off = shl(zext(c, unit), Npad.bit_length() - 1)
    addr = getelementptr(ptbl, off)
    pc = load(bitcast(addr, bit)) # p if borrow else 0
    hi = add(trunc(lshr(v, bit), bit), pc)
    storeN(trunc(v, bit), pd0)
    storeN(hi, pd0, offset=N)
    call(modF, pz, pd0)
    call(modF, getelementptr(pz, offset), pd1)
    ret(Void)

# Fp2 sqr: (z.a, z.b) = (a^2 - b^2, 2 a b) where x = (a, b), b at offset
# limbs from a. Same structure as mcl's gen_fp2_sqr (fp_generator.hpp): two
# calls of the fused Montgomery mul on alloca buffers, no sqrPre (both
# products are cross products, so squaring symmetry cannot be exploited):
#   t1 = 2b, z.b = mul(t1, a) = 2 a b R^(-1)
#   t2 = a + b, t3 = a + p - b (adding p unconditionally avoids a borrow
#     check; p (a + b) vanishes mod p)
#   z.a = mul(t2, t3) = (a^2 - b^2) R^(-1)
# The mul operands are < 2p, so the products are < 4p^2 < p R, which
# requires p < R/4 (the caller checks this nocarry condition). sqr(x, x)
# works in place: when the first mul writes z.b it only reads x.a, which
# does not overlap x.b, and the second mul reads only the t2/t3 copies.
def gen_fp2_sqr(name, mont, mulF, dataVar, offset):
  N = mont.pn
  resetGlobalIdx()
  pz = IntPtr(unit)
  px = IntPtr(unit)
  with Function(name, Void, pz, px):
    pp = bitcast(dataVar, unit)
    pt1 = alloca_(unit, N)
    pt2 = alloca_(unit, N)
    pt3 = alloca_(unit, N)
    a = loadN(px, N)
    b = loadN(px, N, offset=offset)
    p = loadN(pp, N)
    storeN(add(b, b), pt1)
    storeN(add(a, b), pt2)
    storeN(sub(add(a, p), b), pt3)
    call(mulF, getelementptr(pz, offset), pt1, px)
    call(mulF, pz, pt2, pt3)
    ret(Void)

# emit pz[N] = px[xN] mod p (fixed xN) into the current function by
# word-serial Barrett reduction with a two-word reciprocal (common.modp_step).
# pparam points to the parameter block of p (common.modp_param).
# This fixed-length version is kept here only for the benchmark against the
# variable-length common.emit_modp that mcl ships (moved from mcl/src/common.py
# on 2026-09-08).
def emit_modp2(N, xN, pz, px, pparam, mulPv):
  assert xN >= N
  bu = N * unit + unit
  consts = common.modp_consts(unit, N, pparam)
  # r = top N-1 units of x (< p)
  if N == 1:
    r = None
  else:
    r = loadN(px, N - 1, xN - (N - 1))
  for k in range(xN - N, -1, -1):
    w = load(getelementptr(px, k))
    if r is None:
      xx = zext(w, bu)
    else:
      xx = pack([w, r])
      if xx.bit < bu:  # first step: r has N-1 units
        xx = zext(xx, bu)
    r = common.modp_step(unit, N, xx, consts, mulPv)
  storeN(r, pz)

# z[N] = x[xN] mod p (emit_modp2). The p-dependent constants (Qt = Q 2^s,
# np = 2^bit - p) live in the global {name}_param; it is a non-constant
# external global like the p global so that LLVM keeps loading them (the
# function is then the same code for every p with the same N, which is what
# mcl ships as mclb_modp{256,384}). Make it internal+const to let LLVM fold
# them into immediates instead.
def gen_modp2(name, mont, mulUnit, xN, paramVar):
  N = mont.pn
  resetGlobalIdx()
  pz = IntPtr(unit)
  px = IntPtr(unit)
  with Function(name, Void, pz, px) as f:
    pparam = bitcast(paramVar, unit)
    emit_modp2(N, xN, pz, px, pparam, mulUnit)
    ret(Void)
  return f

# uint32_t modp3(Unit *dst, const Unit *src, size_t srcN, const Modp *para):
# the variable-length modp of mcl (common.gen_modp = mclb_modp{256,384} in
# mcl/src/gen.py) under the bench name; para has the layout of {pre}modp2_param.
def gen_modp3(name, mont, mulUnit, xN):
  return common.gen_modp(name, unit, mont.pn, xN, mulUnit)

# ---- invMod (non constant time safegcd, mcl/include/mcl/invmod.hpp) ----
# The LLVM version of the two's complement safegcd (the former
# mcl::inv::twos::exec<N, W>, now kept as the C++ reference in
# misc/invmod_test.cpp; mcl::inv itself is the signed62/signed30 version since
# 2026-09-14): f, g, d, e are W-unit two's complement values (W = N if
# p < 2^(unit N - 2) (the nocarry condition) so that -2p < d, e < p fits,
# otherwise N + 1), the helpers
# (divsteps / update_fg / update_de) are separate functions called from invMod
# as in the C++ version, and the state lives in allocas (promoted to registers
# after inlining). divsteps uses the 8-bit table (-f^-1 mod 256) of
# secp256k1_modinv64_divsteps_62_var as invmod.hpp does.

inv_modL = 0  # unit - 2

# mask (all ones if x < 0) of a signed unit x
def inv_signMask(x):
  return ashr(x, unit - 1)

# z[W+1] = x[W] * a + y[W] * b for two's complement x, y (loaded from px, py)
# and signed units a, b. mulUnit gives the unsigned product x_u a_u (W+1 units);
# x a = x_u a_u - [x<0] (a_u << W unit) - [a<0] (x_u << unit) mod 2^((W+1) unit).
def emit_inv_mulAdd2(W, px, a, py, b, mulUnit, xNonNeg=False):
  bit = W * unit
  bu = bit + unit
  z = add(call(mulUnit, px, a), call(mulUnit, py, b))
  x = loadN(px, W)
  y = loadN(py, W)
  zero = Imm(0, unit)
  if xNonNeg:
    top = select(icmp(slt, y, Imm(0, bit)), b, zero)
  else:
    top = add(select(icmp(slt, x, Imm(0, bit)), a, zero), select(icmp(slt, y, Imm(0, bit)), b, zero))
  z = sub(z, shl(zext(top, bu), bit))
  c = add(and_(x, sext(inv_signMask(a), bit)), and_(y, sext(inv_signMask(b), bit)))
  z = sub(z, shl(zext(c, bu), unit))
  return z

# y[W] = x[W+1] >> modL (the result fits in W units, so lshr + trunc suffices)
def emit_inv_shr(W, x):
  return trunc(lshr(x, inv_modL), W * unit)

# Unit divsteps(Unit *t, Unit eta, Unit f, Unit g) : divsteps_n_matrix
# useBranch : the eta < 0 swap by a branch instead of selects
def gen_inv_divsteps(name, tbl, cttz, useBranch, private):
  resetGlobalIdx()
  pt = IntPtr(unit)
  eta = Int(unit)
  f = Int(unit)
  g = Int(unit)
  with Function(name, Int(unit), pt, eta, f, g, private=private, alwaysinline=True) as func:
    entryL = Label()
    loopL = Label()
    contL = Label()
    exitL = Label()
    zero = Imm(0, unit)
    one = Imm(1, unit)
    L(entryL)
    br(loopL)
    L(loopL)
    etaP = phi((eta, entryL))
    iP = phi((Imm(inv_modL, unit), entryL))
    fP = phi((f, entryL))
    gP = phi((g, entryL))
    uP = phi((one, entryL))
    vP = phi((zero, entryL))
    qP = phi((zero, entryL))
    rP = phi((one, entryL))
    # zeros = min(i, bsf(g)) (i if g == 0): bit i of g | (~0 << i) is set
    zeros = call(cttz, or_(gP, shl(Imm(-1, unit), iP)), Imm(1, 1))
    eta1 = sub(etaP, zeros)
    i1 = sub(iP, zeros)
    g1 = lshr(gP, zeros)
    u1 = shl(uP, zeros)
    v1 = shl(vP, zeros)
    br(icmp(eq, i1, zero), exitL, contL)
    L(contL)
    neg = icmp(slt, eta1, zero)
    if useBranch:
      swapL = Label()
      joinL = Label()
      br(neg, swapL, joinL)
      L(swapL)
      etaN = sub(zero, eta1)
      gN = sub(zero, fP)
      qN = sub(zero, u1)
      rN = sub(zero, v1)
      br(joinL)
      L(joinL)
      eta2 = phi((eta1, contL), (etaN, swapL))
      f2 = phi((fP, contL), (g1, swapL))
      g2 = phi((g1, contL), (gN, swapL))
      u2 = phi((u1, contL), (qP, swapL))
      v2 = phi((v1, contL), (rP, swapL))
      q2 = phi((qP, contL), (qN, swapL))
      r2 = phi((rP, contL), (rN, swapL))
      tailL = joinL
    else:
      eta2 = select(neg, sub(zero, eta1), eta1)
      f2 = select(neg, g1, fP)
      g2 = select(neg, sub(zero, fP), g1)
      u2 = select(neg, qP, u1)
      v2 = select(neg, rP, v1)
      q2 = select(neg, sub(zero, u1), qP)
      r2 = select(neg, sub(zero, v1), rP)
      tailL = contL
    # limit = min(eta + 1, i) (1 <= limit <= modL), mask = the low min(limit, 8) bits
    e1 = add(eta2, one)
    limit = select(icmp(slt, e1, i1), e1, i1)
    idx = lshr(and_(f2, 255), 1)
    tv = zext(load(getelementptr(tbl, idx)), unit)
    mask = and_(lshr(Imm(-1, unit), sub(Imm(unit, unit), limit)), 255)
    w = and_(mul(g2, tv), mask)
    g3 = add(g2, mul(w, f2))
    q3 = add(q2, mul(w, u2))
    r3 = add(r2, mul(w, v2))
    br(loopL)
    etaP.link(eta2, tailL)
    iP.link(i1, tailL)
    fP.link(f2, tailL)
    gP.link(g3, tailL)
    uP.link(u2, tailL)
    vP.link(v2, tailL)
    qP.link(q3, tailL)
    rP.link(r3, tailL)
    L(exitL)
    store(u1, pt)
    store(v1, getelementptr(pt, 1))
    store(qP, getelementptr(pt, 2))
    store(rP, getelementptr(pt, 3))
    ret(eta1)
  return func

# void update_fg(Unit *f, Unit *g, const Unit *t) : f, g are W units
def gen_inv_update_fg(name, W, mulUnit, private):
  resetGlobalIdx()
  pf = IntPtr(unit)
  pg = IntPtr(unit)
  pt = IntPtr(unit, const=True)
  with Function(name, Void, pf, pg, pt, private=private, alwaysinline=True) as func:
    u = load(pt)
    v = load(getelementptr(pt, 1))
    q = load(getelementptr(pt, 2))
    r = load(getelementptr(pt, 3))
    f1 = emit_inv_mulAdd2(W, pf, u, pg, v, mulUnit)
    g1 = emit_inv_mulAdd2(W, pf, q, pg, r, mulUnit)
    storeN(emit_inv_shr(W, f1), pf)
    storeN(emit_inv_shr(W, g1), pg)
    ret(Void)
  return func

# void update_de(Unit *d, Unit *e, const Unit *t, const Unit *im) : d, e are W units
# im : the parameter block twos::Param<N> of misc/invmod_test.cpp (lowM, Mi, M[N+1] zero-extended)
# sd = ud - ((Mi cd) mod 2^modL) as in invmod.hpp (-2M < d, e < M is kept)
def gen_inv_update_de(name, W, mulUnit, private):
  resetGlobalIdx()
  bit = W * unit
  pd = IntPtr(unit)
  pe = IntPtr(unit)
  pt = IntPtr(unit, const=True)
  pim = IntPtr(unit, const=True)
  with Function(name, Void, pd, pe, pt, pim, private=private, alwaysinline=True) as func:
    u = load(pt)
    v = load(getelementptr(pt, 1))
    q = load(getelementptr(pt, 2))
    r = load(getelementptr(pt, 3))
    lowM = load(pim)
    Mi = load(getelementptr(pim, 1))
    pM = getelementptr(pim, 2)
    md = inv_signMask(load(getelementptr(pd, W - 1)))
    me = inv_signMask(load(getelementptr(pe, W - 1)))
    ud = add(and_(u, md), and_(v, me))
    ue = add(and_(q, md), and_(r, me))
    d1 = emit_inv_mulAdd2(W, pd, u, pe, v, mulUnit)
    e1 = emit_inv_mulAdd2(W, pd, q, pe, r, mulUnit)
    di = add(trunc(d1, unit), mul(lowM, ud))
    ei = add(trunc(e1, unit), mul(lowM, ue))
    mask = Imm((1 << inv_modL) - 1, unit)
    sd = sub(ud, and_(mul(Mi, di), mask))
    se = sub(ue, and_(mul(Mi, ei), mask))
    # d = (d1 + M * sd) >> modL
    Mv = loadN(pM, W)
    d1 = add(d1, sub(call(mulUnit, pM, sd), shl(zext(and_(Mv, sext(inv_signMask(sd), bit)), bit + unit), unit)))
    e1 = add(e1, sub(call(mulUnit, pM, se), shl(zext(and_(Mv, sext(inv_signMask(se), bit)), bit + unit), unit)))
    storeN(emit_inv_shr(W, d1), pd)
    storeN(emit_inv_shr(W, e1), pe)
    ret(Void)
  return func

# void invMod(Unit *y, const Unit *x, const Unit *im) : mcl::inv::twos::exec<N, W>
def gen_invMod(name, N, W, divstepsF, updateFgF, updateDeF):
  resetGlobalIdx()
  bit = W * unit
  py = IntPtr(unit)
  px = IntPtr(unit, const=True)
  pim = IntPtr(unit, const=True)
  # y = x (in place) is allowed, so no noalias
  with Function(name, Void, py, px, pim, noalias=False) as func:
    pf = bitcast(alloca_(bit, 1), unit)
    pg = bitcast(alloca_(bit, 1), unit)
    pd = bitcast(alloca_(bit, 1), unit)
    pe = bitcast(alloca_(bit, 1), unit)
    pt = alloca_(unit, 4)
    peta = alloca_(unit, 1)
    pM = getelementptr(pim, 2)
    storeN(loadN(pM, W), pf)
    g = loadN(px, N)
    if W > N:
      g = zext(g, bit)
    storeN(g, pg)
    storeN(Imm(0, bit), pd)
    storeN(Imm(1, bit), pe)
    store(Imm(-1, unit), peta)
    loopL = Label()
    bodyL = Label()
    doneL = Label()
    br(loopL)
    L(loopL)
    g = loadN(pg, W)
    br(icmp(eq, g, Imm(0, bit)), doneL, bodyL)
    L(bodyL)
    mask = Imm((1 << inv_modL) - 1, unit)
    fLow = and_(load(pf), mask)
    gLow = and_(load(pg), mask)
    eta = call(divstepsF, pt, load(peta), fLow, gLow)
    store(eta, peta)
    call(updateFgF, pf, pg, pt)
    call(updateDeF, pd, pe, pt, pim)
    br(loopL)
    L(doneL)
    # normalize : d in (-2M, M) -> [0, M) (negated if f < 0)
    M = loadN(pM, W)
    zero = Imm(0, bit)
    d = loadN(pd, W)
    d = add(d, select(icmp(slt, d, zero), M, zero))
    minus = icmp(slt, loadN(pf, W), zero)
    d = select(minus, sub(zero, d), d)
    d = add(d, select(icmp(slt, d, zero), M, zero))
    if W > N:
      d = trunc(d, N * unit)
    storeN(d, py)
    ret(Void)
  return func

# mulUnit : common.gen_mulPv for N units ; a W-unit one is generated if W > N
def gen_invMod_all(pre, N, W, mulUnit, mulPos, extractHigh, exportHelpers):
  global inv_modL
  inv_modL = unit - 2
  private = not exportHelpers
  if W > N:
    mulUnit = common.gen_mulPv(f'{pre}inv_mulUnit', unit, W, mulPos, extractHigh, private=True, alwaysinline=True)
  # tbl[(f & 255) >> 1] = -f^-1 mod 256 for odd f (negInv256 of invmod.hpp)
  tbl = makeVar(f'{pre}inv_tbl', 8, [(-pow(f, -1, 256)) % 256 for f in range(1, 256, 2)], static=True, const=True)
  cttz = Function(f'llvm.cttz.i{unit}', Int(unit), Int(unit), Int(1))
  declare(cttz)
  updateFgF = gen_inv_update_fg(f'{pre}inv_update_fg', W, mulUnit, private)
  updateDeF = gen_inv_update_de(f'{pre}inv_update_de', W, mulUnit, private)
  for (suf, useBranch) in [('', False), ('_br', True)]:
    divstepsF = gen_inv_divsteps(f'{pre}inv_divsteps{suf}', tbl, cttz, useBranch, private)
    gen_invMod(f'{pre}invMod{suf}', N, W, divstepsF, updateFgF, updateDeF)

def gen_get_prime(name, pStr):
  resetGlobalIdx()
  r = IntPtr(8, const=True)
  with Function(name, r):
    ret(bitcast(pStr, 8))

def main():
  parser = argparse.ArgumentParser(description='gen bint')
  parser.add_argument('-u', type=int, default=64, help='unit bit size (64 or 32)')
  parser.add_argument('-n', type=int, default=0, help='max size of unit')
  parser.add_argument('-p', type=str, default='', help='characteristic of a finite field')
  parser.add_argument('-type', type=str, default='BLS12-381-p', help='elliptic curve type')
  parser.add_argument('-offset', type=int, default=6, help='sizeof(Fp)/sizeof(Uuit)')
  parser.add_argument('-proto', action='store_true', default=False, help='show prototype')
  parser.add_argument('-pre', type=str, default='mcl_fp_', help='prefix of a Fp function name')
  parser.add_argument('-addn', type=int, default=0, help='mad size of add/sub')
  parser.add_argument('-add', action='store_true', default=False, help='add add function')
  parser.add_argument('-sub', action='store_true', default=False, help='add sub function')
  parser.add_argument('-sub_mask', action='store_true', default=False, help='use an and-mask for the conditional +p in sub instead of the {0,p} table (faster on aarch64)')
  parser.add_argument('-mul', action='store_true', default=False, help='add mul function')
  parser.add_argument('-sqr', action='store_true', default=False, help='add sqr function (a call to mul(z, x, x))')
  parser.add_argument('-mul128', action='store_true', default=False, help='add mul128 (fused Montgomery mul with radix-2^128 q lookahead) function')
  parser.add_argument('-mod', action='store_true', default=False, help='add mod (Montgomery reduction) function')
  parser.add_argument('-mod128', action='store_true', default=False, help='add mod128 (radix-2^128 Montgomery reduction) function')
  parser.add_argument('-mulPre', action='store_true', default=False, help='add mulPre function (z[2N] = x*y, no reduction)')
  parser.add_argument('-mulPreWide', action='store_true', default=False, help='add mulPreWide function (mulPre by a single wide LLVM mul, for bench)')
  parser.add_argument('-sqrPre', action='store_true', default=False, help='add sqrPre function (z[2N] = x^2, no reduction)')
  parser.add_argument('-sqrPreWide', action='store_true', default=False, help='add sqrPreWide function (sqrPre by a single wide LLVM mul, for bench)')
  parser.add_argument('-fp2_mul', action='store_true', default=False, help='add Fp2 mul function (Karatsuba + Montgomery reduction)')
  parser.add_argument('-fp2_sqr', action='store_true', default=False, help='add Fp2 sqr function (2 fused Montgomery mul)')
  parser.add_argument('-modp2', action='store_true', default=False, help='add modp2 function (z[N] = x[modp2_bit/unit] mod p, word-serial Barrett)')
  parser.add_argument('-modp2_bit', type=int, default=512, help='input bit size of modp2 (default 512)')
  parser.add_argument('-modp3', action='store_true', default=False, help='add modp3 function (variable-length modp2: dst[N] = src[srcN] mod p for srcN <= modp2_bit/unit, parameter block as an argument)')
  parser.add_argument('-invMod', action='store_true', default=False, help='add invMod (safegcd, the LLVM version of the two\'s complement exec<N, W> of misc/invmod_test.cpp) and invMod_br (divsteps swap by a branch) functions')
  parser.add_argument('-inv_helpers', action='store_true', default=False, help='export the helpers of invMod (inv_divsteps, inv_update_fg, inv_update_de) for the bench tests')

  opt = parser.parse_args()
  if opt.n == 0:
    opt.n = 9 if opt.u == 64 else 17
    opt.addn = 16 if opt.u == 64 else 32
  # the global holding p gets a per-characteristic name so that modules
  # generated for different p can be linked into one executable
  if opt.p == '':
    opt.p = primeTbl[opt.type].p
    opt.pName = f'mcl_{primeTbl[opt.type].c}_p'
  else:
    opt.p = int(opt.p, 0)
    opt.pName = f'{opt.pre}p'
  opt.pre2 = opt.pre[:-1] + '2_'
  if opt.sqrPre and USE_MULPRE_FOR_SQRPRE:
    opt.mulPre = True
  if opt.fp2_mul:
    opt.mulPre = True
    opt.mod = True
  if opt.fp2_sqr:
    opt.mul = True
  if opt.sqr and USE_MUL_FOR_SQR:
    opt.mul = True

  global mont, unit, unit2
  mont = Montgomery(opt.p, opt.u)
  unit = mont.L
  unit2 = mont.L2
  if opt.proto:
    opt.add = True
    opt.sub = True
    opt.mul = True
    opt.sqr = True
    opt.mod = True
    opt.mulPre = True
    opt.sqrPre = True
    opt.fp2_mul = True
    opt.fp2_sqr = True
    opt.modp2 = True
    opt.modp3 = True
    opt.invMod = True
    showPrototype()

  dataVar = makeVar(opt.pName, mont.bit, mont.p, const=False, static=False)
  makeVar('ip', unit, mont.ip, const=True, static=True)
  pStr = makeStrVar('pStr', hex(opt.p))

  gen_get_prime(f'{opt.pre}get_prime', pStr)

  subTbl = None
  if (opt.sub and not opt.sub_mask) or opt.fp2_mul:
    subTbl = makeSubTbl(opt.pre, mont)
  if opt.add:
    gen_fp_add(f'{opt.pre}add', mont.pn, dataVar)
    gen_fp2_add(f'{opt.pre2}add', mont.pn, dataVar, opt.offset)
  if opt.sub:
    gen_fp_sub(f'{opt.pre}sub', mont.pn, subTbl, dataVar, opt.sub_mask)
    gen_fp2_sub(f'{opt.pre2}sub', mont.pn, subTbl, dataVar, opt.sub_mask, opt.offset)

  mulUU = common.gen_mulUU(unit)
  extractHigh = common.gen_extractHigh(unit)
  mulPos = common.gen_mulPos(unit, mulUU)
  # alwaysinline: for N >= 8 clang stops inlining mulUnit into mulPre and the
  # 2N call round-trips cost ~1.7x in throughput (see memo.md 2026-08-31)
  mulUnit = common.gen_mulPv(f'{opt.pre}mulUnit', unit, mont.pn, mulPos, extractHigh, private=True, alwaysinline=True)

  mulF = None
  if opt.mul:
    mulF = gen_mul(f'{opt.pre}mul', mont, dataVar, mulUnit)
  if opt.sqr:
    gen_sqr(f'{opt.pre}sqr', mont, dataVar, mulUnit, mulF)
  mul128F = None
  if opt.mul128 and mont.pn % 2 == 0 and not mont.isFullBit:
    mul128F = gen_mul128(f'{opt.pre}mul128', mont, dataVar, mulUnit)
  modF = None
  if opt.mod:
    modF = gen_mod(f'{opt.pre}mod', mont, dataVar, mulUnit)
  mod128F = None
  if opt.mod128 and mont.pn % 2 == 0 and not mont.isFullBit:
    mod128F = gen_mod128(f'{opt.pre}mod128', mont, dataVar, mulUnit)
  mulPreF = None
  if opt.mulPre:
    mulPreF = gen_mulPre(f'{opt.pre}mulPre', mont.pn, mulUnit)
  if opt.mulPreWide:
    gen_mulPreWide(f'{opt.pre}mulPreWide', mont.pn)
  if opt.sqrPre:
    gen_sqrPre(f'{opt.pre}sqrPre', mont.pn, mulPreF)
  if opt.sqrPreWide:
    gen_sqrPreWide(f'{opt.pre}sqrPreWide', mont.pn)
  if opt.fp2_mul and not mont.isFullBit:
    gen_fp2_mul(f'{opt.pre2}mul', mont, mulPreF, modF, subTbl, opt.offset)
    # radix-2^128 reduction variant; both mods share R = 2^(unit N), so the
    # Montgomery representation is identical and the variants can be mixed
    if mod128F:
      gen_fp2_mul(f'{opt.pre2}mul128', mont, mulPreF, mod128F, subTbl, opt.offset)
  # p < R/4 so that the fused mul accepts operands < 2p
  nocarry = (mont.p >> (unit * mont.pn - 2)) == 0
  if opt.fp2_sqr and not mont.isFullBit and nocarry:
    gen_fp2_sqr(f'{opt.pre2}sqr', mont, mulF, dataVar, opt.offset)
    # radix-2^128 fused-mul variant (see gen_fp2_mul above)
    if mul128F:
      gen_fp2_sqr(f'{opt.pre2}sqr128', mont, mul128F, dataVar, opt.offset)
  if opt.modp2:
    # the parameter block (Qt, np) of modp2; modp3 takes the
    # same block as an argument, so the bench passes this global to it
    param = common.modp_param(mont.p, unit, mont.pn)
    paramVar = makeVar(f'{opt.pre}modp2_param', unit, param, const=False, static=False)
    gen_modp2(f'{opt.pre}modp2', mont, mulUnit, opt.modp2_bit // unit, paramVar)
  if opt.modp3:
    gen_modp3(f'{opt.pre}modp3', mont, mulUnit, opt.modp2_bit // unit)
  if opt.invMod:
    # W = N + 1 (InvModT::wide) unless -2p < d, e < p fits in signed N units
    invW = mont.pn if nocarry else mont.pn + 1
    gen_invMod_all(opt.pre, mont.pn, invW, mulUnit, mulPos, extractHigh, opt.inv_helpers)

  term()

if __name__ == '__main__':
  main()
