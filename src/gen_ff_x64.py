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

# One row of schoolbook multiplication with mulx and w/o adx (rdx = y[i]):
#   A: row = x * y[i]; one add/adc chain combines row[j] = lo[j] + hi[j-1].
#   B: d = c + row (one chain; d has N+1 limbs, all in registers).
# x_at(j) gives the operand of x[j]. c is None on the first row (B is skipped).
# c[j] is None for a limb spilled to S_ct (folded into the chain as adc; such
# j must be >= 1). All registers of c are released. Returns d (N+1 registers).
def mulRow_wo_adx(N, x_at, c, S_ct, alloc, release):
  # A: row = x * y[i]
  L = [None] * N
  hi = None
  for j in range(N):
    prev = hi
    hi = alloc()
    L[j] = alloc()
    mulx(hi, L[j], x_at(j))
    if j > 0:
      add_ex(L[j], prev, j == 1)
      release(prev)
  adc(hi, 0) # row[N]
  # B: d = c + row
  if c is not None:
    for j in range(N):
      if c[j] is None:
        adc(L[j], S_ct) # the spilled limb of c
      else:
        add_ex(L[j], c[j], j == 0)
        release(c[j])
    adc(hi, 0)
  return L + [hi]

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
        S_ct = None
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
        if allInRegs or isFirst:
          x_at = lambda j: ptr(px + j * 8)
        else:
          x_at = S_x
        # A, B: D = c + x * y[i]
        D = mulRow_wo_adx(N, x_at, c, S_ct, alloc, release)
        if isFirst and not allInRegs:
          for j in range(N):
            mov(rax, ptr(px + j * 8))
            mov(S_x(j), rax)
          release(px)
          release(py)
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

# CF:c[n..0] = c[n..0] + p[n-1..0] * q + (CF << (64*n)) where rdx = q.
# c[n] is loaded from the memory operand nxt (the next limb of xy) in the
# shadow of the first mulx. After the add, c[0] = 0 by the choice of q, so
# adox(tt, c[0]) only collects the pending OF-chain carry into tt; the caller
# then reuses the c[0] register as the new top limb (rotatePack).
# The carry-out is kept in the register CF via setc, never through memory.
# tt = hi(p[n-1]*q) has enough headroom for the two adox because p is not
# full bit. use rax, tt
def mulAdd2(c, nxt, pp, tt, CF, addCF, updateCF=True):
  n = len(c)-1
  a = rax
  xor_(a, a) # clear OF and CF
  for i in range(n):
    mulx(tt, a, ptr(pp + i * 8))
    adox(c[i], a)
    if i == 0:
      mov(c[n], nxt)
    if i == n-1:
      break
    adcx(c[i+1], tt)
  adox(tt, c[0]) # tt += OF-chain carry (c[0] = 0)
  if addCF:
    adox(tt, CF) # add the carry-out of the previous iteration
  adcx(c[n], tt) # c[n] += tt + CF-chain carry
  if updateCF:
    setc(CF.changeBit(8))

# body of the Montgomery reduction: z[N] = xy[2N] R^(-1) mod p where
# R = 2^(64N). Port of mcl's gen_fpDbl_modNF (fp_generator.hpp). The N+1 limb
# accumulator pk stays in registers (rotatePack) and the inter-iteration
# carry lives in the register CF, so no carry flag ever goes through memory.
# Uses rax, rdx; pxy is clobbered (reused for the final select).
def mod_body(pz, pxy, pk, CF, tt, pp, mont):
  N = mont.pn
  lea(pp, ptr(rip+'p'))
  xor_(CF, CF)
  load_pm(pk[0:N], pxy)
  for i in range(N):
    mov(rdx, mont.ip)
    imul(rdx, pk[0]) # rdx = q = uint64_t(pk[0] * ip)
    # CF:pk = pk + xy[N+i]<<(64N) + p*q + CF<<(64N), then pk >>= 64
    # (the shift is the rotation: pk[0] = 0 becomes the new top limb)
    mulAdd2(pk, ptr(pxy + (N + i) * 8), pp, tt, CF, i > 0, i < N-1)
    if i < N-1:
      pk = rotatePack(pk)
  pk0 = pk[0] # pk[0] = 0, reused as a temporary
  zp = pk[1:]
  keep = [pxy, rax, rdx, tt, CF, pk0][0:N]
  assert len(keep) == N
  mov_pp(keep, zp)
  sub_pm(zp, pp) # z -= p
  cmovc_pp(zp, keep)
  store_mp(pz, zp)

# Montgomery reduction: z[N] = xy[2N] R^(-1) mod p.
def gen_mod(name, mont):
  N = mont.pn
  assert N <= 6
  align(16)
  with FuncProc(name):
    assert not mont.isFullBit
    with StackFrame(2, N+4, useRDX=True) as sf:
      mod_body(sf.p[0], sf.p[1], sf.t[0:N+1], sf.t[N+1], sf.t[N+2], sf.t[N+3], mont)

# body of mulPre: z[2N] = x[N] * y[N] (schoolbook, no reduction) with
# adcx/adox rows (mulAdd), i.e. gen_mul without the Montgomery step. After
# row i, pk[0] is final (= z[i]) and is stored immediately; rotatePack shifts
# the window. pk has N+1 registers; t, t2 are temporaries. Uses rax, rdx.
def mulPre_body(pz, px, py, pk, t, t2, N):
  for i in range(N):
    mov(rdx, ptr(py + i * 8))
    if i == 0:
      # pk[N..0] = x * y[0]
      mulPack1(pk, px, t)
    else:
      # pk[N..0] = pk[N-1..0] + x * y[i]
      mulAdd(pk, px, t, t2, True)
    mov(ptr(pz + i * 8), pk[0]) # z[i] is final after row i
    pk = rotatePack(pk)
  # the upper half: z[N..2N-1] = pk[0..N-1]
  for j in range(N):
    mov(ptr(pz + (N + j) * 8), pk[j])

# mulPre: z[2N] = x[N] * y[N]. Unlike gen_mul, no final subtraction is
# needed, so N+3 temps suffice even for N=6 (no spill).
def gen_mulPre(name, mont):
  N = mont.pn
  align(16)
  with FuncProc(name):
    with StackFrame(3, N+3, useRDX=True) as sf:
      mulPre_body(sf.p[0], sf.p[1], sf.p[2], sf.t[0:N+1], sf.t[N+1], sf.t[N+2], N)

# mulPre: z[2N] = x[N] * y[N] (schoolbook, no reduction), built from the same
# mulx-only rows (mulRow_wo_adx) as gen_mul_wo_adx but without the Montgomery
# step C. After row i, d[0] is final (= z[i]) and is stored immediately; the
# remaining N limbs are carried to the next row.
def gen_mulPre_wo_adx(name, mont):
  N = mont.pn
  assert N in (4, 6)
  align(16)
  with FuncProc(name):
    # peak usage: carried c (N regs, one spillable) + row under construction
    # (N+2 regs); pz/px/py stay pinned when everything fits in the 10 temps.
    allInRegs = 2*N+2 <= 10
    with StackFrame(3, 10, useRDX=True, stackSizeByte=0 if allInRegs else (N+3)*8) as sf:
      pz = sf.p[0]
      px = sf.p[1]
      py = sf.p[2]
      if allInRegs:
        cSpill = None
        S_ct = None
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
        if allInRegs or isFirst:
          x_at = lambda j: ptr(px + j * 8)
        else:
          x_at = S_x
        # A, B: d = c + x * y[i]
        d = mulRow_wo_adx(N, x_at, c, S_ct, alloc, release)
        if isFirst and not allInRegs:
          for j in range(N):
            mov(rax, ptr(px + j * 8))
            mov(S_x(j), rax)
          release(px)
          release(py)
        if allInRegs:
          zp = pz
        else:
          zp = rax
          mov(zp, S_pz)
        mov(ptr(zp + i * 8), d[0]) # z[i] is final after row i
        release(d[0])
        c = d[1:]
        if isLast:
          # the upper half: z[N..2N-1] = c[0..N-1]
          for j in range(N):
            mov(ptr(zp + (N + j) * 8), c[j])
        elif cSpill is not None:
          # spill right after it is produced: maximum store-to-load slack
          mov(S_ct, c[cSpill])
          release(c[cSpill])
          c[cSpill] = None

# [H:r[n-1]:...:r[0]] <<= 1 (H is assumed to be a fresh register, set to 0).
# mulx does not touch the flags, so the add/adc self-doubling chain is intact.
def shl1(r, H):
  mov(H, 0)
  add(r[0], r[0])
  for i in range(1, len(r)):
    adc(r[i], r[i])
  adc(H, H)

# py[7..0] = px[3..0]^2. Port of fp_generator.hpp sqrPre4NF: accumulate the
# strictly-upper-triangle cross products x[i]*x[j] (i<j) into [t5..t0] (shifted
# down by one limb), double them with shl1, then add the diagonal squares
# x[i]^2 while storing the result. t holds 11 temporaries.
def sqrPre4(py, px, t):
  t0, t1, t2, t3, t4, t5 = t[0:6]
  x0, x1, x2, x3 = t[6:10]
  H = t[10]

  load_pm([x0, x1, x2, x3], px)
  mov(rdx, x0)
  mulx(t3, t2, x3) # (3, 0)
  mulx(rax, t1, x2) # (2, 0)
  add(t2, rax)
  mov(rdx, x1)
  mulx(t4, rax, x3) # (3, 1)
  adc(t3, rax)
  adc(t4, 0) # [t4:t3:t2:t1]
  mulx(rax, t0, x0) # (1, 0)
  add(t1, rax)
  mulx(rdx, rax, x2) # (2, 1)
  adc(t2, rax)
  adc(t3, rdx)
  mov(rdx, x3)
  mulx(t5, rax, x2) # (3, 2)
  adc(t4, rax)
  adc(t5, 0)

  shl1([t0, t1, t2, t3, t4, t5], H)
  mov(rdx, x0)
  mulx(rdx, rax, rdx)
  mov(ptr(py + 8 * 0), rax)
  add(rdx, t0)
  mov(ptr(py + 8 * 1), rdx)
  mov(rdx, x1)
  mulx(rdx, rax, rdx)
  adc(rax, t1)
  mov(ptr(py + 8 * 2), rax)
  adc(rdx, t2)
  mov(ptr(py + 8 * 3), rdx)
  mov(rdx, x2)
  mulx(rdx, rax, rdx)
  adc(rax, t3)
  mov(ptr(py + 8 * 4), rax)
  adc(rdx, t4)
  mov(ptr(py + 8 * 5), rdx)
  mov(rdx, x3)
  mulx(rdx, rax, rdx)
  adc(rax, t5)
  mov(ptr(py + 8 * 6), rax)
  adc(rdx, H)
  mov(ptr(py + 8 * 7), rdx)

# py[11..0] = px[5..0]^2. Port of fp_generator.hpp sqrPre6 (same scheme as
# sqrPre4, px is read from memory since 6 limbs do not fit in registers).
def sqrPre6(py, px, t):
  t0, t1, t2, t3, t4, t5, t6, t7, t8, t9 = t[0:10]
  H = t[10]

  mov(rdx, ptr(px + 8 * 0))
  mulx(t5, t4, ptr(px + 8 * 5)) # [t5:t4] = (5, 0)
  mulx(rax, t3, ptr(px + 8 * 4)) # (4, 0)
  add(t4, rax)
  mov(rdx, ptr(px + 8 * 1))
  mulx(t6, rax, ptr(px + 8 * 5)) # (5, 1)
  adc(t5, rax)
  adc(t6, 0) # [t6:t5:t4:t3]
  mov(rdx, ptr(px + 8 * 0))
  mulx(rax, t2, ptr(px + 8 * 3))
  add(t3, rax)
  mov(rdx, ptr(px + 8 * 1))
  mulx(H, rax, ptr(px + 8 * 4))
  adc(t4, rax)
  adc(t5, H)
  mov(rdx, ptr(px + 8 * 2))
  mulx(t7, rax, ptr(px + 8 * 5))
  adc(t6, rax)
  adc(t7, 0) # [t7:...:t2]

  mov(rdx, ptr(px + 8 * 0))
  mulx(H, t1, ptr(px + 8 * 2))
  adc(t2, H)
  mov(rdx, ptr(px + 8 * 1))
  mulx(H, rax, ptr(px + 8 * 3))
  adc(t3, rax)
  adc(t4, H)
  mov(rdx, ptr(px + 8 * 2))
  mulx(H, rax, ptr(px + 8 * 4))
  adc(t5, rax)
  adc(t6, H)
  mov(rdx, ptr(px + 8 * 3))
  mulx(t8, rax, ptr(px + 8 * 5))
  adc(t7, rax)
  adc(t8, 0) # [t8:...:t1]
  mov(rdx, ptr(px + 8 * 0))
  mulx(H, t0, ptr(px + 8 * 1))
  add(t1, H)
  mov(rdx, ptr(px + 8 * 1))
  mulx(H, rax, ptr(px + 8 * 2))
  adc(t2, rax)
  adc(t3, H)
  mov(rdx, ptr(px + 8 * 2))
  mulx(H, rax, ptr(px + 8 * 3))
  adc(t4, rax)
  adc(t5, H)
  mov(rdx, ptr(px + 8 * 3))
  mulx(H, rax, ptr(px + 8 * 4))
  adc(t6, rax)
  adc(t7, H)
  mov(rdx, ptr(px + 8 * 4))
  mulx(t9, rax, ptr(px + 8 * 5))
  adc(t8, rax)
  adc(t9, 0) # [t9...:t0]
  shl1([t0, t1, t2, t3, t4, t5, t6, t7, t8, t9], H)

  mov(rdx, ptr(px + 8 * 0))
  mulx(rdx, rax, rdx)
  mov(ptr(py + 8 * 0), rax)
  add(t0, rdx)
  mov(ptr(py + 8 * 1), t0)
  mov(rdx, ptr(px + 8 * 1))
  mulx(rdx, rax, rdx)
  adc(t1, rax)
  mov(ptr(py + 8 * 2), t1)
  adc(t2, rdx)
  mov(ptr(py + 8 * 3), t2)
  mov(rdx, ptr(px + 8 * 2))
  mulx(rdx, rax, rdx)
  adc(t3, rax)
  mov(ptr(py + 8 * 4), t3)
  adc(t4, rdx)
  mov(ptr(py + 8 * 5), t4)
  mov(rdx, ptr(px + 8 * 3))
  mulx(rdx, rax, rdx)
  adc(t5, rax)
  mov(ptr(py + 8 * 6), t5)
  adc(t6, rdx)
  mov(ptr(py + 8 * 7), t6)
  mov(rdx, ptr(px + 8 * 4))
  mulx(rdx, rax, rdx)
  adc(t7, rax)
  mov(ptr(py + 8 * 8), t7)
  adc(t8, rdx)
  mov(ptr(py + 8 * 9), t8)
  mov(rdx, ptr(px + 8 * 5))
  mulx(rdx, rax, rdx)
  adc(t9, rax)
  mov(ptr(py + 8 * 10), t9)
  adc(rdx, H)
  mov(ptr(py + 8 * 11), rdx)

# sqrPre: z[2N] = x[N]^2 (no reduction). Dispatches to the hand-scheduled
# sqrPre4/sqrPre6 (mulx + add/adc, no adcx/adox), which fit in 11 temps.
def gen_sqrPre(name, mont):
  N = mont.pn
  assert N in (4, 6)
  align(16)
  with FuncProc(name):
    with StackFrame(2, 11, useRDX=True) as sf:
      py = sf.p[0]
      px = sf.p[1]
      if N == 4:
        sqrPre4(py, px, sf.t)
      else:
        sqrPre6(py, px, sf.t)

# Fused sqr with Montgomery reduction: z[N] = x[N]^2 R^(-1) mod p.
# sqrPre4/sqrPre6 write the 2N-limb square to a stack buffer and mod_body
# reduces it within the same frame, so the two call/ret + prologue pairs of a
# separate sqrPre + mod call sequence are removed (the intermediate still
# goes through the stack; mod_body reads it as memory operands).
def gen_sqr(name, mont):
  N = mont.pn
  assert N in (4, 6)
  align(16)
  with FuncProc(name):
    assert not mont.isFullBit
    # sqrPre needs 11 temps, mod_body N+4 (<= 10): 11 covers both
    with StackFrame(2, 11, useRDX=True, stackSizeByte=2*N*8) as sf:
      pz = sf.p[0]
      px = sf.p[1]
      if N == 4:
        sqrPre4(rsp, px, sf.t)
      else:
        sqrPre6(rsp, px, sf.t)
      # px is dead after sqrPre; reuse it as the xy pointer (mod_body
      # clobbers it, so rsp itself cannot be passed)
      mov(px, rsp)
      mod_body(pz, px, sf.t[0:N+1], sf.t[N+1], sf.t[N+2], sf.t[N+3], mont)

# The register sequence assigned by StackFrame(pNum, tNum, useRDX=True),
# without emitting the prologue: same logic as StackFrame.getRegIdx (rdx is
# replaced by r11 and r11 itself is skipped), on top of getReg() which
# follows the current ABI (SysV or Win64). Local subroutines (gen_mulPreL,
# gen_modL) use it so that their register contract is identical to the
# corresponding public function, while the caller's StackFrame is responsible
# for saving the callee-saved registers. Since the assignment is positional,
# a frame with more arguments/temps assigns the same prefix, so the caller's
# sf.p[i] is the callee's p[i] on both ABIs.
def getFrameRegs(pNum, tNum):
  regs = []
  pos = 0
  while len(regs) < pNum + tNum:
    r = getReg(pos)
    pos += 1
    if r == rdx:
      r = r11
    elif r == r11:
      r = getReg(pos)
      pos += 1
    regs.append(r)
  return (regs[0:pNum], regs[pNum:pNum+tNum])

# local subroutine version of mulPre, called by call(label).
# Contract: (z, x, y) in StackFrame(3, *, useRDX=True).p (rdi, rsi, r11 on
# SysV); clobbers rax, rdx and the temp registers, so the caller's frame
# must have saved the callee-saved ones among them.
def gen_mulPreL(label, mont):
  N = mont.pn
  (p, t) = getFrameRegs(3, N+3)
  align(16)
  L(label)
  mulPre_body(p[0], p[1], p[2], t[0:N+1], t[N+1], t[N+2], N)
  ret()

# local subroutine version of mod (Montgomery reduction).
# Contract: (z, xy) in StackFrame(2, *, useRDX=True).p (rdi, rsi on SysV);
# clobbers rax, rdx and the temp registers (r11 included), so the caller's
# frame must have saved the callee-saved ones among them.
def gen_modL(label, mont):
  N = mont.pn
  assert N <= 6
  assert not mont.isFullBit
  (p, t) = getFrameRegs(2, N+4)
  align(16)
  L(label)
  mod_body(p[0], p[1], t[0:N+1], t[N+1], t[N+2], t[N+3], mont)
  ret()

# Fp2 mul: (z.a, z.b) = (a c - b d, a d + b c) where x = (a, b), y = (c, d),
# each component N limbs in Montgomery form, b at byte offset*8 from a.
# Port of mcl's gen_fp2_mul + fp2Dbl_mulPreL (fp_generator.hpp): Karatsuba
# with 3 mulPre and 2 Montgomery reductions via the local subroutines
# mulPreL/modL (the caller's StackFrame saves the registers they clobber).
#   s = a + b, t = c + d (raw add; no carry out since p is not full bit)
#   d1 = s t, d0 = a c, d2 = b d
#   d1 -= d0; d1 -= d2 (= a d + b c; no borrow since s t >= a c + b d)
#   d0 -= d2 (mod p 2^(64N): raw sub on the low half, then the high half is
#     an fp_sub with the borrow carried in; the result is < p 2^(64N))
#   z.a = mod(d0), z.b = mod(d1)
def gen_fp2_mul(name, mont, offset, mulPreL, modL):
  N = mont.pn
  assert offset >= N
  FpByte = offset * 8
  align(16)
  with FuncProc(name):
    assert not mont.isFullBit
    # stack: saved z, x, y + s[N] + t[N] + d0[2N] + d1[2N] + d2[2N]
    S_z = 0
    S_x = 8
    S_y = 16
    S_s = 24
    S_t = S_s + N * 8
    S_d0 = S_t + N * 8
    S_d1 = S_d0 + N * 16
    S_d2 = S_d1 + N * 16
    with StackFrame(3, 10, useRDX=True, stackSizeByte=S_d2 + N * 16) as sf:
      # sf.p matches the p of mulPreL/modL (positional prefix, see getFrameRegs)
      pz = sf.p[0]
      px = sf.p[1]
      py = sf.p[2]
      mov(ptr(rsp + S_z), pz)
      mov(ptr(rsp + S_x), px)
      mov(ptr(rsp + S_y), py)
      # s = x.a + x.b, t = y.a + y.b
      for (src, dst) in [(px, S_s), (py, S_t)]:
        for i in range(N):
          mov(rax, ptr(src + i * 8))
          add_ex(rax, ptr(src + FpByte + i * 8), i == 0)
          mov(ptr(rsp + dst + i * 8), rax)
      # d1 = s * t
      lea(pz, ptr(rsp + S_d1))
      lea(px, ptr(rsp + S_s))
      lea(py, ptr(rsp + S_t))
      call(mulPreL)
      # d0 = x.a * y.a
      lea(pz, ptr(rsp + S_d0))
      mov(px, ptr(rsp + S_x))
      mov(py, ptr(rsp + S_y))
      call(mulPreL)
      # d2 = x.b * y.b
      lea(pz, ptr(rsp + S_d2))
      mov(px, ptr(rsp + S_x))
      mov(py, ptr(rsp + S_y))
      add(px, FpByte)
      add(py, FpByte)
      call(mulPreL)
      # d1 -= d0; d1 -= d2 (2N limbs each; no borrow out)
      for off in [S_d0, S_d2]:
        for i in range(N * 2):
          mov(rax, ptr(rsp + S_d1 + i * 8))
          sub_ex(rax, ptr(rsp + off + i * 8), i == 0)
          mov(ptr(rsp + S_d1 + i * 8), rax)
      # d0 -= d2 (mod p 2^(64N)); low half: raw sub through rax
      for i in range(N):
        mov(rax, ptr(rsp + S_d0 + i * 8))
        sub_ex(rax, ptr(rsp + S_d2 + i * 8), i == 0)
        mov(ptr(rsp + S_d0 + i * 8), rax)
      # high half: continue the borrow, then add p if it underflowed
      # (the same pointer-cmov trick as gen_fp2_sub)
      t = sf.t[0:N]
      pp = sf.t[N]
      for i in range(N):
        mov(t[i], ptr(rsp + S_d0 + (N + i) * 8))
        sbb(t[i], ptr(rsp + S_d2 + (N + i) * 8))
      lea(rax, ptr(rip + 'zero'))
      lea(pp, ptr(rip + 'p'))
      cmovc(rax, pp)
      for i in range(N):
        add_ex(t[i], ptr(rax + i * 8), i == 0)
        mov(ptr(rsp + S_d0 + (N + i) * 8), t[i])
      # z.a = mod(d0), z.b = mod(d1)
      mov(pz, ptr(rsp + S_z))
      lea(px, ptr(rsp + S_d0))
      call(modL)
      mov(pz, ptr(rsp + S_z))
      add(pz, FpByte)
      lea(px, ptr(rsp + S_d1))
      call(modL)

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
  parser.add_argument('-sqr', action='store_true', default=False, help='add sqr function (fused sqrPre + Montgomery reduction, N=4, 6 only)')
  parser.add_argument('-mulPre', action='store_true', default=False, help='add mulPre function (z[2N] = x*y, no reduction)')
  parser.add_argument('-mulPre_wo_adx', action='store_true', default=False, help='add mulPre function without adcx/adox (N=4, 6 only)')
  parser.add_argument('-mod', action='store_true', default=False, help='add mod (Montgomery reduction) function')
  parser.add_argument('-sqrPre', action='store_true', default=False, help='add sqrPre function (z[2N] = x^2, no reduction, N=4, 6 only)')
  parser.add_argument('-fp2_mul', action='store_true', default=False, help='add Fp2 mul function (Karatsuba + Montgomery reduction)')
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
  if opt.sqr and not mont.isFullBit:
    gen_sqr(f'{opt.pre}sqr', mont)
  if opt.mulPre:
    gen_mulPre(f'{opt.pre}mulPre', mont)
  if opt.mulPre_wo_adx:
    gen_mulPre_wo_adx(f'{opt.pre}mulPre_wo_adx', mont)
  if opt.mod and not mont.isFullBit:
    gen_mod(f'{opt.pre}mod', mont)
  if opt.sqrPre:
    gen_sqrPre(f'{opt.pre}sqrPre', mont)
  if opt.fp2_mul and not mont.isFullBit:
    mulPreL = Label()
    modL = Label()
    gen_mulPreL(mulPreL, mont)
    gen_modL(modL, mont)
    gen_fp2_mul(f'{pre2}mul', mont, opt.offset, mulPreL, modL)

  term()

if __name__ == '__main__':
  main()
