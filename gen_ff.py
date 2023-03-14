from s_xbyak_llvm import *
from mont import *
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
  Int(unit)
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
  with Function(name, z, px, y) as f:
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

def main():
  parser = argparse.ArgumentParser(description='gen bint')
  parser.add_argument('-u', type=int, default=64, help='unit')
  parser.add_argument('-n', type=int, default=0, help='max size of unit')
  parser.add_argument('-p', type=str, default='', help='characteristic of a finite field')
  parser.add_argument('-proto', action='store_true', default=False, help='show prototype')
  parser.add_argument('-pre', type=str, default='mclb_fp_', help='prefix of a function name')
  parser.add_argument('-addn', type=int, default=0, help='mad size of add/sub')
  opt = parser.parse_args()
  if opt.n == 0:
    opt.n = 9 if opt.u == 64 else 17
    opt.addn = 16 if opt.u == 64 else 32
  if opt.p == '':
    # BLS12-381
    opt.p = '0x1a0111ea397fe69a4b1ba7b6434bacd764774b84f38512bf6730d2a0f6b0f6241eabfffeb153ffffb9feffffffffaaab'
  opt.p = eval(opt.p)

  setGlobalParam(opt)
  if opt.proto:
    showPrototype()

  pp = makeVar('p', mont.bit, mont.p, const=True, static=True)
  ip = makeVar('ip', unit, mont.ip, const=True, static=True)
  name = f'{opt.pre}add'
  gen_fp_add(name, mont.pn, pp)
  name = f'{opt.pre}sub'
  gen_fp_sub(name, mont.pn, pp)

  mulUU = gen_mulUU()
  extractHigh = gen_extractHigh()
  mulPos = gen_mulPos(mulUU)
  name = f'{opt.pre}mulUnit'
  gen_mulUnit(name, mont.pn, mulPos, extractHigh)

  term()

if __name__ == '__main__':
  main()
