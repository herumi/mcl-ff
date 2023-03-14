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
  with Function(name, z, x, private=True):
    x = lshr(x, unit)
    z = trunc(x, unit)
    ret(z)

def gen_mulPos(mulUU):
  resetGlobalIdx()
  xy = Int(unit2)
  px = IntPtr(unit)
  y = Int(unit)
  i = Int(unit)
  name = f'mulPos{unit}x{unit}'
  with Function(name, xy, px, y, i, private=True):
    x = load(getelementptr(px, i))
    xy = call(mulUU, x, y)
    ret(xy)

def gen_once():
  mulUU = gen_mulUU()
  gen_extractHigh()
  gen_mulPos(mulUU)

def gen_mcl_fp_add(N, isFullBit=True):
  bit = unit * N
  resetGlobalIdx();
  pz = IntPtr(unit)
  px = IntPtr(unit)
  py = IntPtr(unit)
  pp = IntPtr(unit)
  name = 'mcl_fp_add'
  if not isFullBit:
    name += 'NF'
  name += f'{N}L'
  with Function(name, Void, pz, px, py, pp):
    x = loadN(px, N)
    y = loadN(py, N)
    if isFullBit:
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

def fff(a):
  resetGlobalIdx();
  x = Int(32)
  with Function('sss', x, x):
    p = getelementptr(a, x)
    x = load(p)
    ret(x)

def main():
  parser = argparse.ArgumentParser(description='gen bint')
  parser.add_argument('-u', type=int, default=64, help='unit')
  parser.add_argument('-n', type=int, default=0, help='max size of unit')
  parser.add_argument('-p', type=int, default=0, help='characteristic of a finite field')
  parser.add_argument('-proto', action='store_true', default=False, help='show prototype')
  parser.add_argument('-addn', type=int, default=0, help='mad size of add/sub')
  opt = parser.parse_args()
  if opt.n == 0:
    opt.n = 9 if opt.u == 64 else 17
    opt.addn = 16 if opt.u == 64 else 32
  if opt.p == 0:
    # BLS12-381
    opt.p = 0x1a0111ea397fe69a4b1ba7b6434bacd764774b84f38512bf6730d2a0f6b0f6241eabfffeb153ffffb9feffffffffaaab

  setGlobalParam(opt)
  if opt.proto:
    showPrototype()

  a = makeVar('x', 32, [1, 2, 3, 4], const=True, static=True)
  b = makeVar('p', mont.bit, mont.p, const=True, static=True)
  makeVar('y', 32, 4, static=True)
  fff(a)
  term()
  return
  gen_once()
  gen_mcl_fp_add(3, isFullBit=False)
  term()

if __name__ == '__main__':
  main()
