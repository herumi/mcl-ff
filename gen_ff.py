from s_xbyak_llvm import *
from mont import *
from primetbl import *
import argparse

unit = 0
unit2 = 0
MASK = 0
mont = None

def setGlobalParam(opt):
  global unit, unit2, MASK
  unit = opt.u
  unit2 = unit * 2
  MASK = (1 << unit) - 1

  global mont
  mont = Montgomery(opt.p, unit)

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

def gen_fp_add(name, N, pp):
  bit = unit * N
  resetGlobalIdx();
  pz = IntPtr(unit)
  px = IntPtr(unit)
  py = IntPtr(unit)
  with Function(name, Void, pz, px, py):
    x = loadN(px, N)
    y = loadN(py, N)
    if mont.isFullBit:
      x = zext(x, bit + unit)
      y = zext(y, bit + unit)
      x = add(x, y)
      p = load(pp)
      p = zext(p, bit + unit)
      y = sub(x, p)
      c = trunc(lshr(y, bit), 1)
      x = select(c, x, y)
      x = trunc(x, bit)
      storeN(x, pz)
    else:
      x = add(x, y)
      p = load(pp)
      y = sub(x, p)
      c = trunc(lshr(y, bit - 1), 1)
      x = select(c, x, y)
      storeN(x, pz)
    ret(Void)

def gen_fp_sub(name, N, pp):
  bit = unit * N
  resetGlobalIdx();
  pz = IntPtr(unit)
  px = IntPtr(unit)
  py = IntPtr(unit)
  with Function(name, Void, pz, px, py):
    x = loadN(px, N)
    y = loadN(py, N)
    if mont.isFullBit:
      x = zext(x, bit + 1)
      y = zext(y, bit + 1)

    v = sub(x, y)
    if mont.isFullBit:
      c = trunc(lshr(v, bit), 1)
      v = trunc(v, bit)
    else:
      c = trunc(lshr(v, bit-1), 1)
    p = load(pp)
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

def gen_mul(name, mont, pp, mulUnit):
  ip = mont.ip
  N = mont.pn
  bit = unit * N
  bu = bit + unit
  bu2 = bit + unit * 2
  resetGlobalIdx()
  pz = IntPtr(unit)
  px = IntPtr(unit)
  py = IntPtr(unit)
  with Function(name, Void, pz, px, py):
    pp = bitcast(pp, unit)
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
        q = mul(at, ip)
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
      q = mul(c0, ip)
      pq = call(mulUnit, pp, q)
      t = add(xy, pq)
      t = lshr(t, unit)
      for i in range(1, N):
        y = load(getelementptr(py, i))
        xy = call(mulUnit, px, y)
        t = add(t, xy)
        c0 = trunc(t, unit)
        q = mul(c0, ip)
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
  parser.add_argument('-x64', action='store_true', default=False, help='add x64 to name')
  opt = parser.parse_args()
  if opt.n == 0:
    opt.n = 9 if opt.u == 64 else 17
    opt.addn = 16 if opt.u == 64 else 32
  if opt.p == '':
    opt.p = primeTbl[opt.type]

  setGlobalParam(opt)
  if opt.proto:
    showPrototype()

  pp = makeVar('p', mont.bit, mont.p, const=True, static=True)
  ip = makeVar('ip', unit, mont.ip, const=True, static=True)
  pStr = makeStrVar('pStr', hex(opt.p))

  gen_get_prime(f'{opt.pre}get_prime', pStr)

  name = f'{opt.pre}add'
  gen_fp_add(name, mont.pn, pp)
  name = f'{opt.pre}sub'
  gen_fp_sub(name, mont.pn, pp)

  mulUU = gen_mulUU()
  extractHigh = gen_extractHigh()
  mulPos = gen_mulPos(mulUU)
  name = f'{opt.pre}mulUnit'
  mulUnit = gen_mulUnit(name, mont.pn, mulPos, extractHigh)
  if not opt.x64 or mont.isFullBit:
    name = f'{opt.pre}mul'
    gen_mul(name, mont, pp, mulUnit)

  term()

if __name__ == '__main__':
  main()
