import sys
from s_xbyak import *
from primetbl import *
from mont import *
import argparse

SIMD_BYTE = 64

"""
primeTbl = {
  'BLS12-381-p' : 0x1a0111ea397fe69a4b1ba7b6434bacd764774b84f38512bf6730d2a0f6b0f6241eabfffeb153ffffb9feffffffffaaab,
  'BLS12-381-r' : 0x73eda753299d7d483339d80809a1d80553bda402fffe5bfeffffffff00000001,
  'BN254-p' : 0x2523648240000001ba344d80000000086121000000000013a700000000000013,
  'BN254-r' : 0x2523648240000001ba344d8000000007ff9f800000000010a10000000000000d,
  'p511' : 0x65b48e8f740f89bffc8ab0d15e3e4c4ab42d083aedc88c425afbfcc69322c9cda7aac6c567f35507516730cc1f0b4f25c2721bf457aca8351b81b90533c6c87b,
}
"""

# add(x, y) if noCF is True
# adc(x, y) if noCF is False
def add_ex(x, y, noCF):
  if noCF:
    add(x, y)
  else:
    adc(x, y)

# sub(x, y) if noCF is True
# sbb(x, y) if noCF is False
def sub_ex(x, y, noCF):
  if noCF:
    sub(x, y)
  else:
    sbb(x, y)

def getAt(x, i):
  if type(x) == list:
    return x[i]
  if type(x) == tuple:
    (r, m) = x
    if i < len(r):
      return r[i]
    else:
      return ptr(m + 8 * i)
  raise Exception(f'bad type={type(x)} x={x}, i={i}')

def getNum(x):
  if type(x) == Reg:
    return 1
  if type(x) == Address:
    return 1
  if type(x) == list:
    return len(x)
  if type(x) == tuple:
    (r, m) = x
    return len(r)
  raise Exception(f'bad type={type(x)} x={x}, i={i}')

def make_vec_pm(op, x, addr):
  for i in range(len(x)):
    op(getAt(x, i), ptr(addr + 8 * i))

def make_vec_pp(op, x, y):
  for i in range(len(x)):
#    op(getAt(x, i), getAt(y, i))
    op(x[i], y[i])

# [addr] = x[]
def store_mp(addr, x):
  for i in range(len(x)):
    mov(ptr(addr + 8 * i), x[i])

def load_pm(x, addr):
  make_vec_pm(mov, x, addr)

def mov_pp(x, y):
  make_vec_pp(mov, x, y)

def cmovc_pp(x, y):
  make_vec_pp(cmovc, x, y)

def sub_pm(x, addr):
  n = len(x)
  for i in range(n):
    sub_ex(x[i], ptr(addr + i * 8), i == 0)

def gen_add(name, mont):
  N = mont.pn
  align(16)
  with FuncProc(name):
    assert not mont.isFullBit
    n = min(N*2-2, 11)
    with StackFrame(3, n) as sf:
      pz = sf.p[0]
      px = sf.p[1]
      py = sf.p[2]
      t1 = sf.t[0:N]

      # t1 = x + y
      # t2 = t1 + (2**(64*N)-p)
      # ret = CF ? t2 : t1
      for i in range(N):
        mov(t1[i], ptr(px + i * 8))
        add_ex(t1[i], ptr(py + i * 8), i == 0)
      t2 = sf.t[N:]
      t2.append(px)
      t2.append(py)
      assert len(t2) == N

      negp = 2**(64*N) - mont.p
      for i in range(N):
        mov(t2[i], (negp >> (i*64))%(2**64))
        add_ex(t2[i], t1[i], i == 0)
      for i in range(N):
        cmovnc(t2[i], t1[i])
        mov(ptr(pz + i * 8), t2[i])

# Fp2 add: two independent Fp adds on the [a, b] components, b at byte offset*8 from a.
def gen_fp2_add(name, mont, offset):
  N = mont.pn
  align(16)
  with FuncProc(name):
    assert not mont.isFullBit
    assert N*2 <= 12  # 11 temps + rax
    with StackFrame(3, 11) as sf:
      pz = sf.p[0]
      px = sf.p[1]
      py = sf.p[2]
      t = sf.t[:]
      t.append(rax)
      t1 = t[0:N]
      t2 = t[N:N*2]
      for half in range(2):
        off = half * offset * 8
        # t1 = x + y
        for i in range(N):
          mov(t1[i], ptr(px + off + i * 8))
          add_ex(t1[i], ptr(py + off + i * 8), i == 0)
        # t2 = t1 - p (CF set if t1 < p)
        for i in range(N):
          mov(t2[i], t1[i])
          sub_ex(t2[i], ptr(rip + 'p' + i * 8), i == 0)
        # keep t1 (= x+y) if it underflowed, else use t2 (= x+y-p)
        for i in range(N):
          cmovc(t2[i], t1[i])
          mov(ptr(pz + off + i * 8), t2[i])

# Fp2 sub: two independent Fp subs on the [a, b] components, b at byte offset*8 from a.
# pointer-cmov trick, which is lighter on uops (better throughput) than a 2*N-register select: t = x - y,
# then cmovc picks &p or &zero into rax by the borrow, and t += *rax folds the conditional +p as a memory add.
# Uses only N+1 temps + rax, so both halves reuse the same registers.
# Correct for full-bit p too (t + p is taken mod 2^(64N)).
def gen_fp2_sub(name, mont, offset):
  N = mont.pn
  align(16)
  with FuncProc(name):
    with StackFrame(3, N+1) as sf:
      pz = sf.p[0]
      px = sf.p[1]
      py = sf.p[2]
      t = sf.t[0:N]
      pp = sf.t[N]
      for half in range(2):
        off = half * offset * 8
        # t = x - y (CF set if x < y)
        for i in range(N):
          mov(t[i], ptr(px + off + i * 8))
          sub_ex(t[i], ptr(py + off + i * 8), i == 0)
        # rax = borrow ? &p : &zero  (lea/cmovc do not disturb the borrow flag)
        lea(rax, ptr(rip + 'zero'))
        lea(pp, ptr(rip + 'p'))
        cmovc(rax, pp)
        # t += *rax (= x-y when x >= y, else x-y+p) and store
        for i in range(N):
          add_ex(t[i], ptr(rax + i * 8), i == 0)
        for i in range(N):
          mov(ptr(pz + off + i * 8), t[i])

def gen_sub(name, mont):
  N = mont.pn
  align(16)
  with FuncProc(name):
    assert not mont.isFullBit
    n = min(N*2-2, 11)
    with StackFrame(3, n) as sf:
      pz = sf.p[0]
      px = sf.p[1]
      py = sf.p[2]
      t1 = sf.t[0:N]
      for i in range(N):
        mov(t1[i], ptr(px + i * 8))
        sub_ex(t1[i], ptr(py + i * 8), i == 0)
      sbb(rax, rax) # -1 if x<y else 0
      t2 = sf.t[N:]
      t2.append(px)
      t2.append(py)
      assert len(t2) == N
      # t2 = p if x<y else 0
      for i in range(N):
        mov(t2[i], (mont.p >> (i*64))%(2**64))
        and_(t2[i], rax)
      for i in range(N):
        add_ex(t1[i], t2[i], i == 0)
        mov(ptr(pz + i*8), t1[i])

#  c[n..0] = c[n-1..0] + px[n-1..0] * rdx if is_cn_zero = True
#  c[n..0] = c[n..0] + px[n-1..0] * rdx if is_cn_zero = False
#  use rdx, t, t2
def mulAdd(c, px, t, t2, is_cn_zero):
  n = len(c)-1
  if is_cn_zero:
    xor_(c[n], c[n])
  else:
    xor_(t, t) # clear ZF
  for i in range(n):
    mulx(t, t2, ptr(px + i * 8))
    adox(c[i], t2)
    if i == n-1:
      break
    adcx(c[i + 1], t)
  adox(c[n], t)
  adc(c[n], 0)

#  c[n..0] = px[n-1..0] * rdx
#  use t
def mulPack1(c, px, t):
  n = len(c)-1
  mulx(c[1], c[0], ptr(px + 0 * 8))
  for i in range(1, n):
    mulx(c[i + 1], t, ptr(px + i * 8))
    add_ex(c[i], t, i == 1)
  adc(c[n], 0)

def montgomery1(mont, c, px, pp, t1, t2, isFirst):
  d = rdx
  if isFirst:
    # c[n..0] = px[n-1..0] * rdx
    mulPack1(c, px, t1)
  else:
    # c[n..0] = c[n-1..0] + px[n-1..0] * rdx because of not fuill bit
    mulAdd(c, px, t1, t2, True)

  mov(d, mont.ip)
  imul(d, c[0]) # d = q = uint64_t(d * c[0])
  # c[n..0] += p * q because of not fuill bit
  mulAdd(c, pp, t1, t2, False)

def rotatePack(pk):
  t = pk[1:]
  t.append(pk[0])
  return t

# Montgomery mul(x, y)
def gen_mul(name, mont):
  N = mont.pn
  align(16)
  with FuncProc(name):
    assert not mont.isFullBit
    with StackFrame(3, N+3, useRDX=True) as sf:
      pz = sf.p[0]
      px = sf.p[1]
      py = sf.p[2]
      pk = sf.t[0:N+1]
      t = sf.t[N+1]
      t2 = sf.t[N+2]

      lea(rax, ptr(rip+'p'))
      for i in range(N):
        mov(rdx, ptr(py + i * 8))
        montgomery1(mont, pk, px, rax, t, t2, i == 0)
        if i < N - 1:
          pk = rotatePack(pk)
      keep = [pk[0], px, py, rdx]
      pk = pk[1:]
      keep.extend(sf.t[N+1:])
      keep = keep[0:N]
      assert len(keep) == N
      mov_pp(keep, pk)
      sub_pm(pk, rax) # z - p
      cmovc_pp(pk, keep)
      store_mp(pz, pk)

# Montgomery mul(x, y) w/mulx and w/o adx(adcx/adox) is faster than w/adx.
# Loop invariant: the accumulator c (< 2p, N limbs) is in registers except
# possibly c[N-1] (see below). One iteration (rdx = y[i]):
#   A: row = x * y[i]; one chain combines row[j] = lo[j] + hi[j-1].
#   B: d = c + row (one chain; d has N+1 limbs, all in registers).
#   C: q = d[0] * ip; chain1 t[j] = lo(p[j]*q) + d[j] (t[0] = 0, dropped;
#      its carry is (d[0] != 0), computed by neg without waiting for mulx),
#      chain2 c'[j] = t[j+1] + hi(p[j]*q), which doubles as the /2^64 shift.
def gen_mul_wo_adx(name, mont):
  N = mont.pn
  assert N in (4, 6)
  align(16)
  with FuncProc(name):
    assert not mont.isFullBit
    # With N=6, registers are insufficient, so part of c is spilled to the stack.
    allInRegs = 2*N+5 <= 13
    with StackFrame(3, 10, useRDX=True, stackSizeByte=0 if allInRegs else (N+3)*8) as sf:
      pz = sf.p[0]
      px = sf.p[1]
      py = sf.p[2]
      if allInRegs:
        cSpill = None
        pool = sf.t[:]
      else:
        S_pz = ptr(rsp + 0)
        S_py = ptr(rsp + 8)
        cSpill = N-1 # which limb of c to spill; must be >= 1
        S_ct = ptr(rsp + 16) # c[cSpill] between iterations
        def S_x(j):
          return ptr(rsp + 24 + j * 8)
        mov(S_pz, pz)
        mov(S_py, py)
        pool = [pz] + sf.t[:]
      def alloc():
        return pool.pop()
      def release(r):
        pool.append(r)
      c = None
      for i in range(N):
        isFirst = i == 0
        isLast = i == N-1
        if allInRegs or isFirst:
          mov(rdx, ptr(py + i * 8)) # rdx = y[i]
        else:
          mov(rdx, S_py)
          mov(rdx, ptr(rdx + i * 8)) # rdx = y[i]
        # A: row = x * y[i]
        L = [None] * N
        hi = None
        for j in range(N):
          prev = hi
          hi = alloc()
          L[j] = alloc()
          if allInRegs or isFirst:
            mulx(hi, L[j], ptr(px + j * 8))
          else:
            mulx(hi, L[j], S_x(j))
          if j > 0:
            add_ex(L[j], prev, j == 1)
            release(prev)
        adc(hi, 0) # row[N]
        if isFirst and not allInRegs:
          for j in range(N):
            mov(rax, ptr(px + j * 8))
            mov(S_x(j), rax)
          release(px)
          release(py)
        # B: d = c + row
        if not isFirst:
          for j in range(N):
            if j == cSpill:
              adc(L[j], S_ct)
            else:
              add_ex(L[j], c[j], j == 0)
              release(c[j])
          adc(hi, 0)
        D = L + [hi]
        # C: q = d[0] * ip ; c' = (d + q*p)/2^64
        mov(rdx, mont.ip)
        imul(rdx, D[0]) # rdx = q
        # t[0] = lo(p[0]*q) + d[0] = 0 by the choice of q; only its carry
        # matters and lo(p[0]*q) = -d[0] mod 2^64, so CF = (d[0] != 0), which
        # is what neg computes. This starts chain1 without waiting for mulx.
        neg(D[0])
        release(D[0])
        PH = [None] * N
        T = [None] * (N+1)
        for j in range(N):
          PH[j] = alloc()
          lo = alloc()
          mulx(PH[j], lo, ptr(rip + 'p' + j * 8))
          if j == 0:
            release(lo) # lo(p[0]*q) is not needed, see above
          else:
            adc(lo, D[j])
            release(D[j])
            T[j] = lo
        adc(D[N], 0)
        T[N] = D[N]
        c = [None] * N
        for j in range(N):
          c[j] = T[j+1]
          add_ex(c[j], PH[j], j == 0)
          release(PH[j])
          if j == cSpill and not isLast:
            # spill right after it is produced: maximum store-to-load slack
            mov(S_ct, c[j])
            release(c[j])
            c[j] = None
      # c < 2p; output c - p if c >= p
      keep = []
      for j in range(N):
        keep.append(alloc())
      mov_pp(keep, c)
      sub_pm(c, rip + 'p')
      cmovc_pp(c, keep)
      if not allInRegs:
        pz = rax
        mov(pz, S_pz)
      store_mp(pz, c)

def main():
  parser = getDefaultParser('gen bint')
  parser.add_argument('-p', type=str, default='', help='characteristic of a finite field')
  parser.add_argument('-type', type=str, default='BLS12-381-p', help='elliptic curve type')
  parser.add_argument('-pre', type=str, default='mcl_fp_', help='prefix of a function name')
  parser.add_argument('-offset', type=int, default=6, help='sizeof(Fp)/sizeof(Unit)')
  parser.add_argument('-add', action='store_true', default=False, help='add add function')
  parser.add_argument('-sub', action='store_true', default=False, help='add sub function')
  parser.add_argument('-mul', action='store_true', default=False, help='add mul function')
  parser.add_argument('-mul_wo_adx', action='store_true', default=False, help='add mul function without adcx/adox (N=4, 6 only)')
  opt = parser.parse_args()

  init(opt)
  opt.u = 64
  opt.proto = False
  if opt.p == '':
    opt.p = primeTbl[opt.type]

  mont = Montgomery(opt.p, opt.u)
  if opt.proto:
    showPrototype()

  segment('data')
  makeVar('p', mont.bit, mont.p, const=True, static=True)
  makeVar('zero', mont.bit, 0, const=True, static=True)
  makeVar('ip', opt.u, mont.ip, const=True, static=True)
  makeVar('vmask', 64, (1<<52)-1, const=True, static=True)
  segment('text')

  pre2 = opt.pre[:-1] + '2_'
  if opt.add:
    name = f'{opt.pre}add'
    gen_add(name, mont)
    gen_fp2_add(f'{pre2}add', mont, opt.offset)
  if opt.sub:
    name = f'{opt.pre}sub'
    gen_sub(name, mont)
    gen_fp2_sub(f'{pre2}sub', mont, opt.offset)
  if opt.mul and not mont.isFullBit:
    name = f'{opt.pre}mul'
    gen_mul(name, mont)
  if opt.mul_wo_adx and not mont.isFullBit:
    name = f'{opt.pre}mul_wo_adx'
    gen_mul_wo_adx(name, mont)

  term()

if __name__ == '__main__':
  main()
