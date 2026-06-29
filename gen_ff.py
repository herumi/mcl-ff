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
  name = f'mcl_fp_addPre{N}'
  with Function(name, Void, pz, px, py):
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
  name = f'mul{unit}x{unit}L'
  with Function(name, z, x, y, private=True) as f:
    x = zext(x, unit2)
    y = zext(y, unit2)
    z = mul(x, y)
    ret(z)
  return f

def gen_extractHigh():
  resetGlobalIdx()
  z = Int(unit)
  x = Int(unit2)
  name = f'extractHigh{unit}'
  with Function(name, z, x, private=True) as f:
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
  name = f'mulPos{unit}x{unit}'
  with Function(name, xy, px, y, i, private=True) as f:
    x = load(getelementptr(px, i))
    xy = call(mulUU, x, y)
    ret(xy)
  return f

def gen_once():
  mulUU = gen_mulUU()
  gen_extractHigh()
  gen_mulPos(mulUU)

# Derive an i64* to p[0] from the prime-data global.
# const mode: dataVar is the wide p constant; ip is an immediate (ipBase None).
# -var-p mode: dataVar is [N+1 x i64] laid out as {ip, p[N]} (same memory layout
# as mcl's struct { uint64_t ip; uint64_t p[N]; }); ipBase points to ip (elem 0).
def derivePtr(dataVar, var_p):
  if var_p:
    base = bitcast(dataVar, unit)
    pp = getelementptr(base, 1)
    return pp, base
  pp = bitcast(dataVar, unit)
  return pp, None

def gen_fp_add(name, N, dataVar, var_p):
  bit = unit * N
  resetGlobalIdx();
  pz = IntPtr(unit)
  px = IntPtr(unit)
  py = IntPtr(unit)
  with Function(name, Void, pz, px, py):
    pp, _ = derivePtr(dataVar, var_p)
    # volatile: keep the operand loads unfused so store-forwarded inputs
    # (common in dependency chains) do not pay the folded-load latency.
    x = loadN(px, N, volatile=True)
    y = loadN(py, N, volatile=True)
    if mont.isFullBit:
      x = zext(x, bit + unit)
      y = zext(y, bit + unit)
      x = add(x, y)
      p = loadN(pp, N)
      p = zext(p, bit + unit)
      y = sub(x, p)
      c = trunc(lshr(y, bit), 1)
      x = select(c, x, y)
      x = trunc(x, bit)
      storeN(x, pz)
    else:
      x = add(x, y)
      p = loadN(pp, N)
      y = sub(x, p)
      c = trunc(lshr(y, bit - 1), 1)
      x = select(c, x, y)
      storeN(x, pz)
    ret(Void)

def gen_fp_sub(name, N, dataVar, var_p):
  bit = unit * N
  resetGlobalIdx();
  pz = IntPtr(unit)
  px = IntPtr(unit)
  py = IntPtr(unit)
  with Function(name, Void, pz, px, py):
    pp, _ = derivePtr(dataVar, var_p)
    x = loadN(px, N, volatile=True)
    y = loadN(py, N, volatile=True)
    if mont.isFullBit:
      x = zext(x, bit + 1)
      y = zext(y, bit + 1)

    v = sub(x, y)
    if mont.isFullBit:
      c = trunc(lshr(v, bit), 1)
      v = trunc(v, bit)
    else:
      c = trunc(lshr(v, bit-1), 1)
    p = loadN(pp, N)
    c = select(c, p, Imm(0, bit))
    v = add(v, c)
    storeN(v, pz)
    ret(Void)

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

def gen_mul(name, mont, dataVar, mulUnit, var_p, arg_p=False):
  N = mont.pn
  bit = unit * N
  bu = bit + unit
  bu2 = bit + unit * 2
  resetGlobalIdx()
  pz = IntPtr(unit)
  px = IntPtr(unit)
  py = IntPtr(unit)
  args = [pz, px, py]
  if arg_p:
    # 4th argument points to struct { uint64_t ip; uint64_t p[N]; }, i.e. the
    # same [ip, p[N]] layout as -var-p but passed in by the caller instead of
    # referenced from a fixed global.
    pParam = IntPtr(unit)
    args.append(pParam)
  with Function(name, Void, *args):
    if arg_p:
      ipval = load(pParam)
      pp = getelementptr(pParam, 1)
    else:
      pp, ipBase = derivePtr(dataVar, var_p)
      if var_p:
        ipval = load(ipBase)
      else:
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

def gen_get_prime(name, pStr):
  resetGlobalIdx()
  r = IntPtr(8)
  with Function(name, r):
    ret(bitcast(pStr, 8))

def main():
  parser = argparse.ArgumentParser(description='gen bint')
  parser.add_argument('-u', type=int, default=64, help='unit bit size (64 or 32)')
  parser.add_argument('-n', type=int, default=0, help='max size of unit')
  parser.add_argument('-p', type=str, default='', help='characteristic of a finite field')
  parser.add_argument('-type', type=str, default='BLS12-381-p', help='elliptic curve type')
  parser.add_argument('-proto', action='store_true', default=False, help='show prototype')
  parser.add_argument('-pre', type=str, default='mcl_fp_', help='prefix of a function name')
  parser.add_argument('-addn', type=int, default=0, help='mad size of add/sub')
  parser.add_argument('-add', action='store_true', default=False, help='add add function')
  parser.add_argument('-sub', action='store_true', default=False, help='add sub function')
  parser.add_argument('-mul', action='store_true', default=False, help='add mul function')
  parser.add_argument('-var-p', dest='var_p', action='store_true', default=False, help='reference p/ip from a runtime [ip, p[N]] array instead of immediates')
  parser.add_argument('-arg-p', dest='arg_p', action='store_true', default=False, help='pass a pointer to struct { uint64_t ip; uint64_t p[N]; } as the 4th argument of mul instead of using a global')

  opt = parser.parse_args()
  if opt.n == 0:
    opt.n = 9 if opt.u == 64 else 17
    opt.addn = 16 if opt.u == 64 else 32
  if opt.p == '':
    opt.p = primeTbl[opt.type]

  global mont, unit, unit2
  mont = Montgomery(opt.p, opt.u)
  unit = mont.L
  unit2 = mont.L2
  if opt.proto:
    opt.add = True
    opt.sub = True
    opt.mul = True
    showPrototype()

  if opt.var_p:
    mask = (1 << unit) - 1
    limbs = [(mont.p >> (unit * i)) & mask for i in range(mont.pn)]
    dataVar = makeVar(f'{opt.pre}param', unit, [mont.ip] + limbs, static=False, const=False)
  else:
    dataVar = makeVar('p', mont.bit, mont.p, const=True, static=True)
    makeVar('ip', unit, mont.ip, const=True, static=True)
  pStr = makeStrVar('pStr', hex(opt.p))

  gen_get_prime(f'{opt.pre}get_prime', pStr)

  if opt.add:
    name = f'{opt.pre}add'
    gen_fp_add(name, mont.pn, dataVar, opt.var_p)
  if opt.sub:
    name = f'{opt.pre}sub'
    gen_fp_sub(name, mont.pn, dataVar, opt.var_p)

  mulUU = gen_mulUU()
  extractHigh = gen_extractHigh()
  mulPos = gen_mulPos(mulUU)
  name = f'{opt.pre}mulUnit'
  mulUnit = gen_mulUnit(name, mont.pn, mulPos, extractHigh)

  if opt.mul:
    name = f'{opt.pre}mul'
    gen_mul(name, mont, dataVar, mulUnit, opt.var_p, opt.arg_p)

  term()

if __name__ == '__main__':
  main()
