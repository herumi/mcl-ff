from s_xbyak_llvm import *
from mont import *
from primetbl import *
import argparse

unit = 0
unit2 = 0
mont = None

def gen_add(N):
  bit = unit * N
  resetGlobalIdx()
  pz = IntPtr(unit)
  px = IntPtr(unit)
  py = IntPtr(unit)
  with Function(f'mcl_fp_addPre{N}', Void, pz, px, py):
    x = zext(loadN(px, N), bit + unit)
    y = zext(loadN(py, N), bit + unit)
    z = add(x, y)
    storeN(trunc(z, bit), pz)
    r = trunc(lshr(z, bit), unit)
    ret(Void)

def gen_mulUU():
  resetGlobalIdx();
  z = Int(unit2)
  x = Int(unit)
  y = Int(unit)
  with Function(f'mul{unit}x{unit}L', z, x, y, private=True) as f:
    x = zext(x, unit2)
    y = zext(y, unit2)
    z = mul(x, y)
    ret(z)
  return f

def gen_extractHigh():
  resetGlobalIdx()
  z = Int(unit)
  x = Int(unit2)
  with Function(f'extractHigh{unit}', z, x, private=True) as f:
    x = lshr(x, unit)
    z = trunc(x, unit)
    ret(z)
  return f

def gen_mulPos(mulUU):
  resetGlobalIdx()
  xy = Int(unit2)
  px = IntPtr(unit)
  y = Int(unit)
  i = Int(unit)
  with Function(f'mulPos{unit}x{unit}', xy, px, y, i, private=True) as f:
    x = load(getelementptr(px, i))
    xy = call(mulUU, x, y)
    ret(xy)
  return f

def gen_once():
  mulUU = gen_mulUU()
  gen_extractHigh()
  gen_mulPos(mulUU)

def gen_add_raw(x, y, p, isFullBit):
  bit = x.bit
  if isFullBit:
    x = zext(x, bit + unit)
    y = zext(y, bit + unit)
    x = add(x, y)
    p = zext(p, bit + unit)
    y = sub(x, p)
    c = trunc(lshr(y, bit), 1)
    x = select(c, x, y)
    x = trunc(x, bit)
  else:
    x = add(x, y)
    y = sub(x, p)
    c = trunc(lshr(y, bit - 1), 1)
    x = select(c, x, y)
  return x

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
    x = gen_add_raw(x, y, p, mont.isFullBit)
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
      x = gen_add_raw(x, y, p, mont.isFullBit)
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
  if isFullBit:
    x = zext(x, bit + unit)
    y = zext(y, bit + unit)
    v = sub(x, y)
    c = trunc(lshr(v, bit), 1)
    v = trunc(v, bit)
  else:
    v = sub(x, y)
    c = trunc(lshr(v, bit - 1), 1)
  off = shl(zext(c, unit), Npad.bit_length() - 1)
  addr = getelementptr(ptbl, off)
  p = load(bitcast(addr, bit))
  v = add(v, p)
  return v

def gen_fp_sub(name, N, subTbl):
  bit = unit * N
  resetGlobalIdx();
  pz = IntPtr(unit)
  px = IntPtr(unit)
  py = IntPtr(unit)
  with Function(name, Void, pz, px, py):
    tbl, Npad = subTbl
    ptbl = bitcast(tbl, unit)
    x = loadN(px, N, volatile=True)
    y = loadN(py, N, volatile=True)
    v = gen_sub_raw_tbl(x, y, ptbl, Npad, mont.isFullBit)
    storeN(v, pz)
    ret(Void)

def gen_fp2_sub(name, N, subTbl, offset):
  bit = unit * N
  resetGlobalIdx();
  pz = IntPtr(unit)
  px = IntPtr(unit)
  py = IntPtr(unit)
  with Function(name, Void, pz, px, py):
    tbl, Npad = subTbl
    ptbl = bitcast(tbl, unit)
    for i in range(2):
      x = loadN(px, N, offset=i*offset, volatile=True)
      y = loadN(py, N, offset=i*offset, volatile=True)
      v = gen_sub_raw_tbl(x, y, ptbl, Npad, mont.isFullBit)
      storeN(v, pz, offset=i*offset)

    ret(Void)

# split x into (high, low) with low being sizeL bits
def split(x, sizeL):
  H = lshr(x, sizeL)
  H = trunc(H, x.bit - sizeL)
  L = trunc(x, sizeL)
  return (H, L)

# return [xs[n-1]:xs[n-2]:...:xs[0]]
def pack(xs):
  x = xs[0]
  for y in xs[1:]:
    shift = x.bit
    size = x.bit + y.bit
    x = zext(x, size)
    y = zext(y, size)
    y = shl(y, shift)
    x = or_(x, y)
  return x

def gen_mulUnit(name, N, mulPos, extractHigh):
  bit = unit * N
  bu = bit + unit
  resetGlobalIdx()
  z = Int(bu)
  px = IntPtr(unit)
  y = Int(unit)
  with Function(name, z, px, y, private=True) as f:
    L = []
    H = []
    for i in range(N):
      xy = call(mulPos, px, y, Imm(i, unit))
      L.append(trunc(xy, unit))
      H.append(call(extractHigh, xy))

    LL = pack(L)
    HH = pack(H)
    LL = zext(LL, bu)
    HH = zext(HH, bu)
    HH = shl(HH, unit)
    z = add(LL, HH)
    ret(z)
  return f

def gen_mul(name, mont, dataVar, mulUnit):
  N = mont.pn
  bit = unit * N
  bu = bit + unit
  bu2 = bit + unit * 2
  resetGlobalIdx()
  pz = IntPtr(unit)
  px = IntPtr(unit)
  py = IntPtr(unit)
  with Function(name, Void, pz, px, py):
    pp = bitcast(dataVar, unit)
    ipval = mont.ip
    if mont.isFullBit:
      for i in range(N):
        y = load(getelementptr(py, i))
        xy = call(mulUnit, px, y)
        if i == 0:
          a = zext(xy, bu2)
          at = trunc(xy, unit)
        else:
          xy = zext(xy, bu2)
          a = add(s, xy)
          at = trunc(a, unit)
        q = mul(at, ipval)
        pq = call(mulUnit, pp, q)
        pq = zext(pq, bu2)
        t = add(a, pq)
        s = lshr(t, unit)

      s = trunc(s, bu)
      p = zext(loadN(pp, N), bu)
      vc = sub(s, p)
      c = trunc(lshr(vc, bit), 1)
      z = select(c, s, vc)
      z = trunc(z, bit)
      storeN(z, pz)
    else:
      y = load(py)
      xy = call(mulUnit, px, y)
      c0 = trunc(xy, unit)
      q = mul(c0, ipval)
      pq = call(mulUnit, pp, q)
      t = add(xy, pq)
      t = lshr(t, unit)
      for i in range(1, N):
        y = load(getelementptr(py, i))
        xy = call(mulUnit, px, y)
        t = add(t, xy)
        c0 = trunc(t, unit)
        q = mul(c0, ipval)
        pq = call(mulUnit, pp, q)
        t = add(t, pq)
        t = lshr(t, unit)
      t = trunc(t, bit)
      vc = sub(t, loadN(pp, N))
      c = trunc(lshr(vc, bit - 1), 1)
      z = select(c, t, vc)
      storeN(z, pz)
    ret(Void)

# Montgomery reduction: z = xy R^-1 mod p where xy has 2N units.
def gen_mod(name, mont, dataVar, mulUnit):
  N = mont.pn
  bit = unit * N
  bu = bit + unit
  bu2 = bit + unit * 2
  resetGlobalIdx()
  pz = IntPtr(unit)
  pxy = IntPtr(unit)
  with Function(name, Void, pz, pxy) as f:
    pp = bitcast(dataVar, unit)
    ipval = mont.ip
    p = loadN(pp, N)
    t = loadN(pxy, N)
    H = None
    for i in range(N):
      if N == 1:
        q = mul(t, ipval)
      else:
        q = mul(trunc(t, unit), ipval)
      pq = call(mulUnit, pp, q)
      if i > 0:
        H = zext(H, bu)
        H = shl(H, bit)
        pq = add(pq, H)
      nxt = load(getelementptr(pxy, N + i))
      t = pack([t, nxt])
      t = zext(t, bu2)
      pq = zext(pq, bu2)
      t = add(t, pq)
      t = lshr(t, unit)
      t = trunc(t, bu)
      H, t = split(t, bit)
    if mont.isFullBit:
      p = zext(p, bu)
      t = pack([t, H])
      vc = sub(t, p)
      c = trunc(lshr(vc, bit), 1)
      z = select(c, t, vc)
      z = trunc(z, bit)
    else:
      vc = sub(t, p)
      c = trunc(lshr(vc, bit - 1), 1)
      z = select(c, t, vc)
    storeN(z, pz)
    ret(Void)
  return f

# pz[2N] = px[N] * py[N] (no reduction). Port of gen.py:generic_fpDbl_mul of
# mcl: schoolbook rows x * y[i] accumulated in the bit+unit accumulator t, whose
# bottom unit is final after each row and is stored immediately.
def mulPre_raw(pz, px, py, N, mulUnit):
  if N == 1:
    x = zext(load(px), unit2)
    y = zext(load(py), unit2)
    storeN(mul(x, y), pz)
    return
  y = load(py)
  xy = call(mulUnit, px, y)
  store(trunc(xy, unit), pz)
  t = lshr(xy, unit)
  for i in range(1, N):
    y = load(getelementptr(py, i))
    xy = call(mulUnit, px, y)
    t = add(t, xy)
    if i < N - 1:
      storeN(trunc(t, unit), pz, i)
      t = lshr(t, unit)
  storeN(t, pz, N - 1)

# mulPre: pz[2N] = px[N] * py[N] (no reduction).
def gen_mulPre(name, N, mulUnit):
  resetGlobalIdx()
  pz = IntPtr(unit)
  px = IntPtr(unit)
  py = IntPtr(unit)
  with Function(name, Void, pz, px, py) as f:
    mulPre_raw(pz, px, py, N, mulUnit)
    ret(Void)
  return f

# If True then sqrPre(z, x) is a call to mulPre(z, x, x), as in mcl's
# gen_mcl_fpDbl_sqrPre, instead of the dedicated schedule below.
# Counter-intuitively this is the fastest of the three variants we tried: when
# the mulPre body is inlined (or written as a plain mul(x, x) on 2N limbs) LLVM
# spots that x[i]*x[j] == x[j]*x[i] and drops 36 mulx down to 21, but the carry
# chain it then builds is long enough to lose the win. Clang keeps the call as
# a tail jump, so nothing is paid for it.
USE_MULPRE_FOR_SQRPRE = True

# sqrPre: pz[2N] = px[N]^2 (no reduction).
# Port of fp_generator.hpp sqrPre4/sqrPre6: accumulate the strictly-upper-
# triangle cross products sum_{i<j} x[i]*x[j] << unit*(i+j), double them (each
# off-diagonal term appears twice by symmetry), then add the diagonal squares
# x[i]^2 << unit*2*i. LLVM lowers the wide shl/add chain to a mulx/adc sequence.
def gen_sqrPre(name, N, mulPreF):
  bit = unit * N
  bit2 = bit * 2
  resetGlobalIdx()
  pz = IntPtr(unit)
  px = IntPtr(unit)
  with Function(name, Void, pz, px):
    if USE_MULPRE_FOR_SQRPRE:
      call(mulPreF, pz, px, px)
      ret(Void)
      return
    x = [load(getelementptr(px, i)) for i in range(N)]
    cross = None
    for i in range(N):
      for j in range(i + 1, N):
        xij = mul(zext(x[i], unit2), zext(x[j], unit2))
        xij = shl(zext(xij, bit2), unit * (i + j))
        cross = xij if cross is None else add(cross, xij)
    z = shl(cross, 1)
    for i in range(N):
      xii = mul(zext(x[i], unit2), zext(x[i], unit2))
      xii = shl(zext(xii, bit2), unit * 2 * i)
      z = add(z, xii)
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
  parser.add_argument('-mul', action='store_true', default=False, help='add mul function')
  parser.add_argument('-mod', action='store_true', default=False, help='add mod (Montgomery reduction) function')
  parser.add_argument('-mulPre', action='store_true', default=False, help='add mulPre function (z[2N] = x*y, no reduction)')
  parser.add_argument('-sqrPre', action='store_true', default=False, help='add sqrPre function (z[2N] = x^2, no reduction)')
  parser.add_argument('-fp2_mul', action='store_true', default=False, help='add Fp2 mul function (Karatsuba + Montgomery reduction)')

  opt = parser.parse_args()
  if opt.n == 0:
    opt.n = 9 if opt.u == 64 else 17
    opt.addn = 16 if opt.u == 64 else 32
  if opt.p == '':
    opt.p = primeTbl[opt.type]
  opt.pre2 = opt.pre[:-1] + '2_'
  if opt.sqrPre and USE_MULPRE_FOR_SQRPRE:
    opt.mulPre = True
  if opt.fp2_mul:
    opt.mulPre = True
    opt.mod = True

  global mont, unit, unit2
  mont = Montgomery(opt.p, opt.u)
  unit = mont.L
  unit2 = mont.L2
  if opt.proto:
    opt.add = True
    opt.sub = True
    opt.mul = True
    opt.mod = True
    opt.mulPre = True
    opt.sqrPre = True
    opt.fp2_mul = True
    showPrototype()

  dataVar = makeVar('p', mont.bit, mont.p, const=False, static=False)
  makeVar('ip', unit, mont.ip, const=True, static=True)
  pStr = makeStrVar('pStr', hex(opt.p))

  gen_get_prime(f'{opt.pre}get_prime', pStr)

  subTbl = None
  if opt.sub or opt.fp2_mul:
    subTbl = makeSubTbl(opt.pre, mont)
  if opt.add:
    gen_fp_add(f'{opt.pre}add', mont.pn, dataVar)
    gen_fp2_add(f'{opt.pre2}add', mont.pn, dataVar, opt.offset)
  if opt.sub:
    gen_fp_sub(f'{opt.pre}sub', mont.pn, subTbl)
    gen_fp2_sub(f'{opt.pre2}sub', mont.pn, subTbl, opt.offset)

  mulUU = gen_mulUU()
  extractHigh = gen_extractHigh()
  mulPos = gen_mulPos(mulUU)
  mulUnit = gen_mulUnit(f'{opt.pre}mulUnit', mont.pn, mulPos, extractHigh)

  if opt.mul:
    gen_mul(f'{opt.pre}mul', mont, dataVar, mulUnit)
  modF = None
  if opt.mod:
    modF = gen_mod(f'{opt.pre}mod', mont, dataVar, mulUnit)
  mulPreF = None
  if opt.mulPre:
    mulPreF = gen_mulPre(f'{opt.pre}mulPre', mont.pn, mulUnit)
  if opt.sqrPre:
    gen_sqrPre(f'{opt.pre}sqrPre', mont.pn, mulPreF)
  if opt.fp2_mul and not mont.isFullBit:
    gen_fp2_mul(f'{opt.pre2}mul', mont, mulPreF, modF, subTbl, opt.offset)

  term()

if __name__ == '__main__':
  main()
