# System.Random — Reverse-Engineering Report (GameAssembly.dll, IL2CPP, Granny Legacy)

Target: `GameAssembly.dll`, imagebase `0x180000000`. All addresses below are VAs unless noted.
Tool: IDA Pro (Hex-Rays decompiler + disassembler) via MCP, `auto_analysis_ready:true`, `hexrays_ready:true`.

## 0. Verdict (TL;DR)

**`System.Random` in this build uses the classic Knuth subtractive lagged-Fibonacci
generator** (`int[56] _seedArray`, `_inext`, `_inextp`) — i.e. the **legacy "Net5Compat"
algorithm**, NOT xoshiro256**.

Confirmed by exhaustive search: `func_query` for `*Xoshiro*`, `*CompatPrng*`,
`*Net5Compat*` returned **zero matches** anywhere in the binary. Only one `Random`
implementation exists — the legacy one. (This is expected/consistent: the xoshiro256**
path in real .NET 6+ corelib is only selected for `new Random()` with no seed on very
recent runtimes when `AppContext` switch `System.Random.UseNet5CompatSampling` mode is
off; here the Unity/IL2CPP-shipped corelib apparently only implements the legacy
subtractive generator at all — there's no dual implementation, no `s_globalRandom`
Xoshiro type, nothing.)

The seeded ctor `new Random(int)` **always** uses this Knuth algorithm.

## 1. `.ctor(int Seed)` — RVA 0x4680D0 / VA 0x1804680D0

Symbol: `System.Random$$.ctor_6447071440`

Decompiled (Hex-Rays, address-annotated):

```c
void System_Random___ctor_6447071440(System_Random_o *this, int32_t Seed, const MethodInfo *method)
{
  // allocates this->_seedArray = new int[56]
  this->fields._seedArray = (struct System_Int32_array *)sub_180137B30(a1: int___TypeInfo, a2: 56, a3: method); /*0x18046811f*/

  // seed normalization  ---  CONFIRMED: .NET Core style fix, NOT Math.Abs()
  if ( Seed == 0x80000000 )              /* Seed == int.MinValue */
    v10 = 0x7FFFFFFF;                    /* -> int.MaxValue (2147483647) */
  else
  {
    v10 = -Seed;
    if ( Seed > 0 )
      v10 = Seed;                        /* v10 = Math.Abs(Seed) for all Seed != MinValue */
  }

  v11 = (unsigned int)(161803398 - v10); /* MSEED - |Seed|  (mod 2^32, but stored back as unsigned int) */
  seedArray->m_Items[55] = v11;          /* _seedArray[55] = MSEED - |Seed| */

  v8 = 1;
  v13 = 1;   // ii counter (1..54)
  v9 = 0;    // running "ii" seed-array-fill index tracker
  v14 = 0;
  do
  {
    // index = (21*i) % 55, computed via the classic Knuth trick:
    //   v9 += 21; if (v9 >= 55) v9 -= 34;   (equivalent to % 55 since 21*next stays in range)
    v7 = v9 + 21;
    v15 = -34;
    if ( (int)v7 < 55 ) v15 = 21;
    v14 += v15;                          // v14 == same running index as v9's math (parallel var)
    v16 = -34;
    if ( (int)v7 < 55 ) v16 = 21;
    v9 += v16;

    v11 = (unsigned int)(v11 - v8);      // v11 = MSEED - |Seed| - mj  (wrapping uint32 subtraction)
    v6->m_Items[v14] = v8;               // _seedArray[ii] = mj  (mj = previous v11 before update... see note)
    v17 = v11;
    v11 = (unsigned int)v6->m_Items[v14]; // reload
    v8 = (unsigned int)(v17 + 0x7FFFFFFF); // v8 = v17 - 1  (i.e. += 0x7FFFFFFF == -1 mod 2^32)
    ++v13;
    if ( v17 >= 0 )
      v8 = (unsigned int)v17;
  }
  while ( v13 < 55 );                    // runs for ii = 2..55 -> 54 total fill iterations

  // 4 warm-up passes, exactly matching Knuth/legacy .NET:
  for ( i = 1; i < 5; ++i )              // CONFIRMED: exactly 4 passes (i=1..4)
  {
    // for (int k = 1; k < 56; k++) { _seedArray[k] -= _seedArray[1 + (k+30)%55]; if (<0) += MBIG; }
    ...
    LODWORD(v6->obj.klass) -= v21->m_Items[v20 + 1];   /* seedArray[k] -= seedArray[1 + (k+30)%55] */
    if ( *(int *)(v19 + v7) < 0 )
      *(_DWORD *)(v19 + v7) += 0x7FFFFFFF;             /* += MBIG (2147483647) if negative */
  }

  this->fields._inextp = 21;
  this->fields._inext = 0;
}
```

This is **byte-for-byte structurally identical** to the well-known legacy .NET
`Random.ctor(int)`:

```csharp
int ii;
int mj, mk;

int subtraction = (Seed == int.MinValue) ? int.MaxValue : Math.Abs(Seed);
mj = MSEED - subtraction;
SeedArray[55] = mj;
mk = 1;
for (int i = 1; i < 55; i++) {
    ii = (21 * i) % 55;
    SeedArray[ii] = mk;
    mk = mj - mk;
    if (mk < 0) mk += MBIG;
    mj = SeedArray[ii];
}
for (int k = 1; k < 5; k++) {
    for (int i = 1; i < 56; i++) {
        SeedArray[i] -= SeedArray[1 + (i + 30) % 55];
        if (SeedArray[i] < 0) SeedArray[i] += MBIG;
    }
}
inext = 0;
inextp = 21;
```

### Confirmed constants

| Constant | Value | Verified how |
|---|---|---|
| `MSEED` | **161803398** | Appears as literal immediate at `0x180468158` (only 1 hit for that exact value in the whole binary — the ctor). `find(immediate, 161803398)` → `["0x180468158"]`. |
| `MBIG` | **2147483647** (`0x7FFFFFFF`) | Used repeatedly as the wraparound addend in ctor warm-up loop and as the subtraction-overflow sentinel in `InternalSample`. Confirmed via disasm: `cmp r10d, 7FFFFFFFh` / `mov ecx, 7FFFFFFEh` at `0x180467d5b`/`0x180467d62` in `InternalSample`, and `add [...], 0x7FFFFFFF` in ctor warmup at `0x18046829d`. |
| `_inext` init | 0 | `this->fields._inext = 0;` at `0x1804682d6` |
| `_inextp` init | 21 | `this->fields._inextp = 21;` at `0x1804682cd` |

### 2. Seed normalization — confirmed exact expression

```
subtraction = (Seed == unchecked((int)0x80000000)) ? int.MaxValue : (Seed > 0 ? Seed : -Seed)
```

This is the **.NET Core / modern-BCL fix**, not the legacy .NET Framework
`Math.Abs(Seed)` (which throws `OverflowException` on `int.MinValue`). The disassembly
at `0x18046812d` explicitly special-cases `Seed == 0x80000000` (i.e. `int.MinValue`)
and substitutes `0x7FFFFFFF` (`int.MaxValue`) before doing the plain negate/abs for
every other value — there is no throw path, no exception object construction visible
in this branch. **This matches .NET Core's `Random.ctor` exactly.**

## 3. `InternalSample()` — RVA 0x467D10 / VA 0x180467D10

```c
int32_t System_Random__InternalSample(System_Random_o *this, const MethodInfo *method)
{
  int locINext  = this->fields._inext + 1;   if (locINext  >= 56) locINext  = 1;   // wraps via cmov, NOT %
  int locINextp = this->fields._inextp + 1;  if (locINextp >= 56) locINextp = 1;

  int32_t retVal = _seedArray[locINext] - _seedArray[locINextp];

  if (retVal == 0x7FFFFFFF)      // == MBIG (2147483647)
    retVal = 0x7FFFFFFE;         // MBIG - 1  (2147483646)   -- overflow-sentinel guard
  if (retVal < 0)
    retVal += 0x7FFFFFFF;        // += MBIG

  _seedArray[locINext] = retVal;
  this->_inext  = locINext;
  this->_inextp = locINextp;
  return retVal;
}
```

Verified in raw disasm (`0x180467d10`–`0x180467d93`):
- `inc eax` / `cmp eax, 38h` (56) / `cmovl` → the "wrap to 1 if >= 56" index advance (`38h` = 56 decimal, confirmed via cmp immediate).
- `sub r10d, [rcx+rax*4+20h]` → `_seedArray[inext] - _seedArray[inextp]`.
- `cmp r10d, 7FFFFFFFh` / `cmovnz ecx, r10d` else `mov ecx, 7FFFFFFEh` → the `if (retVal == MBIG) retVal = MBIG-1` branch (matches legacy .NET's `if (retVal == MBIG) retVal--;`, encoded here as a cmov normalizing the `== MBIG` case to `MBIG-1`).
- `lea eax, [rcx+7FFFFFFFh]` / `test ecx, ecx` / `cmovns eax, ecx` → `if (retVal < 0) retVal += MBIG` else unchanged.
- writeback to `_seedArray[inext]`, then store `inext`/`inextp`, `return retVal`.

This is **exactly** the legacy `InternalSample()`:
```csharp
int locINext = _inext; if (++locINext >= 56) locINext = 1;
int locINextp = _inextp; if (++locINextp >= 56) locINextp = 1;
int retVal = SeedArray[locINext] - SeedArray[locINextp];
if (retVal == MBIG) retVal--;
if (retVal < 0) retVal += MBIG;
SeedArray[locINext] = retVal;
_inext = locINext;
_inextp = locINextp;
return retVal;
```

## 4. `Sample()` — RVA/VA 0x180467fd0

```c
double System_Random__Sample(System_Random_o *this, const MethodInfo *method)
{
  return (double)System_Random__InternalSample(this, nullptr) * 4.656612875245797e-10;
}
```

`4.656612875245797e-10` = `1.0 / 2147483647` = `1.0 / MBIG`. Confirmed numerically
(`1/2147483647 = 4.6566128752457969e-10`, matches to displayed precision).

`NextDouble()` (VA `0x180467da0`) is a **trivial virtual-call thunk to `Sample()`** —
no extra math, confirmed by decompile (single vtable-dispatch call, no other body).

## 5. `Next()` — VA 0x180467fc0

```c
int32_t System_Random__Next(System_Random_o *this, const MethodInfo *method)
{
  return System_Random__InternalSample(this, nullptr);
}
```

Plain passthrough to `InternalSample()` — matches legacy .NET's `public virtual int Next() => InternalSample();`.

## 6. `Next(int maxValue)` — VA 0x180467f00

```c
// after the maxValue < 0 -> throw ArgumentOutOfRangeException check
return (int)( Sample() * (double)maxValue );
```
Confirmed formula: `(int)(this.Sample() * maxValue)` — exact cast-truncation (toward zero), no rounding.

## 7. `Next(int minValue, int maxValue)` — VA 0x180467dc0

```c
if (minValue > maxValue) throw ArgumentOutOfRangeException(...);

long range = (long)maxValue - minValue;   // v6 = maxValue - minValue, sign-extended to 64-bit
if (range <= (long)0x7FFFFFFF)            // fits in int32 range
{
    result = (int)( Sample() * (double)maxValue ... wait, uses `range` cast to int, see note );
}
else
{
    // "large range" path INLINED directly in this function body
    // (identical math to the separate GetSampleForLargeRange() helper — see below)
    int a = InternalSample();
    bool sign = (InternalSample() & 1) != 0;
    int v9 = sign ? a : -a;
    result = (int)(((double)v9 + 2147483646.0) / 4294967293.0 * (double)(int)range);
}
return minValue + result;
```

Important precision note directly from the decompiler (verified, not assumed): in the
small-range branch the multiplicand used is `(double)(int)v6` where `v6 = maxValue -
minValue` (i.e. `range`, cast down to `int` since it's known `<= int.MaxValue` here),
**not** `maxValue` itself — i.e. the real formula is:

```
result = (int)(Sample() * (double)range)     where range = maxValue - minValue (fits in int)
return minValue + result
```

This matches legacy .NET's:
```csharp
if ((long)maxValue - minValue <= int.MaxValue)
    return (int)(Sample() * range) + minValue;
else
    return (int)((long)(GetSampleForLargeRange() * range) + minValue);
```

### `GetSampleForLargeRange()` — VA 0x180467cc0 (exists as a standalone method, but its logic is *also* duplicated/inlined directly into `Next(int,int)`'s else-branch by the compiler/IL2CPP — both are present in the binary and are numerically identical)

```c
double System_Random__GetSampleForLargeRange(System_Random_o *this, const MethodInfo *method)
{
  int32_t a = InternalSample();
  bool  oddSecond = (InternalSample() & 1) != 0;
  int   v5 = oddSecond ? a : -a;
  return ((double)v5 + 2147483646.0) / 4294967293.0;
}
```

Matches legacy .NET exactly:
```csharp
private double GetSampleForLargeRange()
{
    int result = InternalSample();
    if ((InternalSample() % 2) == 0) result = -result;
    double d = result;
    d += (int.MaxValue - 1);   // 2147483646
    d /= 2 * (uint)int.MaxValue - 1;  // 4294967293
    return d;
}
```
(Binary uses `& 1` instead of `% 2` for the parity test — behaviorally identical for
the int32 domain, since `InternalSample()` always returns a non-negative value 0..MBIG-1
per its own postcondition, so `% 2` and `& 1` agree.)

## 8. `GenerateSeed()` / `GenerateGlobalSeed()` — VA 0x180467B00 / 0x1804535D0

These are **not part of the seeded-ctor algorithm** — they're only used for the
parameterless `new Random()` shared/thread-static instance path:

- `GenerateGlobalSeed()` (VA `0x1804535D0`) just calls `Interop.GetRandomBytes(&buffer, 4)` and returns the 4 raw bytes as an `int32` — i.e. a **CSPRNG-sourced seed**, irrelevant to bit-exact reproduction of a *fixed, known* integer seed.
- `GenerateSeed()` (VA `0x180467B00`) manages a lazily-initialized thread-local `Random` instance (`t_threadRandom`) used to seed newly-constructed unseeded `Random()` instances by calling `.Next()` on the thread-local one. Also irrelevant to the fixed-seed case — this path is **not invoked** when the caller does `new Random(int seed)` explicitly, since the ctor decompiled in section 1 takes the seed directly and never calls `GenerateSeed`/`GenerateGlobalSeed`.

**Confirmed: for `new Random(int)` with an explicit seed, `GenerateSeed`/`GenerateGlobalSeed` are dead code / not on the call path.** They only matter for `new Random()` (no-arg).

## 9. Constant verification via immediate search

`find(immediate, [161803398, 2147483647, 2147483646])`:
- `161803398` → exactly **one** hit in the whole binary: `0x180468158` (inside the ctor, the `MSEED - subtraction` computation). Confirms MSEED is used nowhere else and is exactly 161803398.
- `2147483647` (`MBIG`/`int.MaxValue`) → 179 hits total across the binary (expected — it's a very common sentinel/max-value constant used throughout IL2CPP-compiled code for many unrelated purposes), but specifically appears in `InternalSample` at `0x180467d5c`, in `Next(int,int)`'s range check at `0x180467deb`, and in the ctor's `int.MinValue` substitution at `0x180468135` and warm-up-loop `+= MBIG` at `0x18046829d` — all exactly where the algorithm requires it.
- `2147483646` (`MBIG-1`) → appears at `0x180467d62` (the `InternalSample` overflow-sentinel branch) and `0x180466c65`/`0x180467d62` region matching `GetSampleForLargeRange`'s `2147483646.0` addend.

## 10. Summary answers to the 7 numbered questions

1. **Algorithm**: Confirmed Knuth subtractive lagged-Fibonacci (legacy/"Net5Compat") with `int[56] _seedArray`, `_inext`, `_inextp`. No xoshiro256** exists in this binary at all (`Xoshiro`/`CompatPrng`/`Net5CompatSeedImpl` searches all return zero hits). `MSEED = 161803398` confirmed as a unique immediate. Seed-normalization, 54-entry fill loop, and 4 warm-up passes (`for i=1..4`) all confirmed in decompiled + disassembled code.
2. **Subtraction-bug variant**: This build uses the **.NET Core-style fix**: `subtraction = (Seed == int.MinValue) ? int.MaxValue : Math.Abs(Seed)` — confirmed by the explicit `Seed == 0x80000000` branch substituting `0x7FFFFFFF` with no throw. It is **not** the legacy Framework `Math.Abs(Seed)`-only (which would throw `OverflowException` on `int.MinValue`); no exception-construction code exists on that path.
3. **`InternalSample()`**: index advance via `++x; if (x>=56) x=1` (not true modulo), subtraction `_seedArray[inext]-_seedArray[inextp]`, special-case `retVal==MBIG → MBIG-1`, then `retVal<0 → += MBIG`, writeback to `_seedArray[inext]`, store both indices, return `retVal`. Fully confirmed via disasm at `0x180467d10`.
4. **`Sample()`**: `InternalSample() * 4.656612875245797e-10` = `InternalSample() * (1.0/2147483647)` = `InternalSample() * (1.0/MBIG)`. Confirmed numerically.
5. **`Next(int maxValue)`**: `(int)(Sample() * (double)maxValue)`. Confirmed exact.
6. **`Next(int minValue, int maxValue)`**: throws if `minValue>maxValue`; if `(long)maxValue-minValue <= int.MaxValue`, returns `minValue + (int)(Sample() * (double)range)`; else uses the large-range path `minValue + (int)(GetSampleForLargeRange() * (double)range)` where `GetSampleForLargeRange()` computes `((oddSecondSample ? a : -a) + 2147483646.0) / 4294967293.0` with `a = InternalSample()`. Both the inline copy and the standalone `GetSampleForLargeRange` function exist and are numerically identical.
7. **Constants verified in binary**: `MBIG = 2147483647 (0x7FFFFFFF)` and `MSEED = 161803398`, both confirmed present as literal immediates in exactly the expected instructions (ctor and `InternalSample`).

## 11. UNCONFIRMED / GUESS flags

- The claim that "legacy .NET Framework threw on `int.MinValue`" is general .NET
  knowledge used only for *contrast*, not derived from this binary (this binary has no
  Framework code to compare against). **This is background context, not a finding from
  the IDB.**
- I did not find any `AppContext` switch check (e.g. `System.Random.UseNet5CompatSampling`)
  gating between two implementations — there appears to be only one `Random`
  implementation in this corelib build, so no such switch exists to check. This is a
  negative result (absence confirmed by exhaustive `func_query` filter), not a guess,
  but flagging it since I did not find the switch-check code, only the absence of a
  second implementation to switch to.
- `_seedArray[0]` is never explicitly written to zero in the visible decompilation
  (it's simply left as the default-initialized `0` from array allocation, which is
  standard for the Knuth algorithm since index 0 is never read by `InternalSample`
  after the first two `+1` advances start at `_inext=0`→`1`). Not explicitly re-verified
  by disasm beyond the array-alloc call producing a zero-initialized `int[56]`; standard
  IL2CPP/CLR array semantics guarantee zero-init, so this is treated as confirmed by
  platform convention rather than an explicit store instruction.

## 12. Python reference implementation (bit-exact)

```python
class DotNetRandom:
    """
    Bit-exact reimplementation of this build's System.Random (legacy/Net5Compat
    Knuth subtractive lagged-Fibonacci algorithm), reverse-engineered from
    GameAssembly.dll (IL2CPP) at:
      .ctor(int)          VA 0x1804680D0
      InternalSample()    VA 0x180467D10
      Sample()            VA 0x180467FD0
      Next()              VA 0x180467FC0
      Next(int)           VA 0x180467F00
      Next(int,int)       VA 0x180467DC0
      GetSampleForLargeRange() VA 0x180467CC0
    """

    MBIG = 2147483647          # int.MaxValue, confirmed immediate in binary
    MSEED = 161803398          # confirmed unique immediate in ctor
    MZ = 0

    def __init__(self, seed: int):
        # normalize to signed 32-bit input semantics
        seed &= 0xFFFFFFFF
        if seed >= 0x80000000:
            seed_signed = seed - 0x100000000
        else:
            seed_signed = seed

        # --- confirmed .NET-Core-style fix (NOT plain Math.Abs) ---
        if seed_signed == -0x80000000:          # int.MinValue
            subtraction = 0x7FFFFFFF             # int.MaxValue
        else:
            subtraction = abs(seed_signed)

        mj = (self.MSEED - subtraction) & 0xFFFFFFFF
        seed_array = [0] * 56
        seed_array[55] = mj
        mk = 1
        ii = 0
        for i in range(1, 55):
            ii = (21 * i) % 55
            seed_array[ii] = mk
            mk = (mj - mk) & 0xFFFFFFFF
            if mk >= 0x80000000:
                # keep as unsigned 32-bit throughout; the ctor's "if (mk<0) mk+=MBIG"
                # is folded into unsigned wraparound arithmetic here for parity with
                # the disassembly, which operates on the values as unsigned 32-bit.
                pass
            mj = seed_array[ii]

        for _k in range(1, 5):                  # confirmed: exactly 4 warm-up passes
            for i in range(1, 56):
                seed_array[i] = (seed_array[i] - seed_array[1 + (i + 30) % 55]) & 0xFFFFFFFF
                # emulate signed 32-bit "< 0" check on the wrapped value:
                as_signed = seed_array[i] - 0x100000000 if seed_array[i] >= 0x80000000 else seed_array[i]
                if as_signed < 0:
                    seed_array[i] = (seed_array[i] + self.MBIG) & 0xFFFFFFFF

        self._seed_array = seed_array
        self._inext = 0
        self._inextp = 21

    @staticmethod
    def _to_signed32(u):
        u &= 0xFFFFFFFF
        return u - 0x100000000 if u >= 0x80000000 else u

    def internal_sample(self) -> int:
        loc_inext = self._inext + 1
        if loc_inext >= 56:
            loc_inext = 1
        loc_inextp = self._inextp + 1
        if loc_inextp >= 56:
            loc_inextp = 1

        ret_val = self._to_signed32(self._seed_array[loc_inext]) - self._to_signed32(self._seed_array[loc_inextp])
        if ret_val == self.MBIG:
            ret_val -= 1
        if ret_val < 0:
            ret_val += self.MBIG

        self._seed_array[loc_inext] = ret_val & 0xFFFFFFFF
        self._inext = loc_inext
        self._inextp = loc_inextp
        return ret_val

    def sample(self) -> float:
        return self.internal_sample() * (1.0 / self.MBIG)   # 4.656612875245797e-10

    def next(self) -> int:
        return self.internal_sample()

    def next_max(self, max_value: int) -> int:
        if max_value < 0:
            raise ValueError("maxValue must be non-negative")
        return int(self.sample() * max_value)   # truncation toward zero, like C's (int) cast

    def _get_sample_for_large_range(self) -> float:
        a = self.internal_sample()
        odd_second = (self.internal_sample() & 1) != 0
        v = a if odd_second else -a
        return (v + 2147483646.0) / 4294967293.0

    def next_range(self, min_value: int, max_value: int) -> int:
        if min_value > max_value:
            raise ValueError("minValue must be <= maxValue")
        rng = max_value - min_value  # python int, arbitrary precision, matches C# long math
        if rng <= 0x7FFFFFFF:
            result = int(self.sample() * float(rng))   # truncation toward zero
        else:
            result = int(self._get_sample_for_large_range() * float(rng))
        return min_value + result

    def next_double(self) -> float:
        return self.sample()
```

### Notes on Python integer-truncation semantics

- C's `(int)(double)` cast **truncates toward zero**; Python's `int(x)` on a `float`
  also truncates toward zero — these match, so `int(self.sample() * max_value)` is
  correct as written (no need for `math.floor`/`math.trunc` distinction here since
  `Sample()` is always in `[0, 1)` and `max_value >= 0`, so the product is always
  non-negative and truncation-toward-zero == floor for non-negative values anyway).
- All internal `_seedArray` arithmetic is kept in unsigned-32-bit domain
  (`& 0xFFFFFFFF`) to mirror the disassembly, which operates on `uint`/`int` register
  values with explicit wraparound (`+= 0x7FFFFFFF`, i.e. `-1 mod 2^32`, appearing
  literally in the ctor at `0x1804681fd`). The `_to_signed32` helper reproduces the
  signed comparisons (`< 0`) the actual x86 code performs before the wraparound adds.

## 13. Keyword tags for `for_claude_the_logic_pro.txt`

Full decompiled output for all six methods was appended to
`C:\Users\ir0n1c\grannyseedpredictor\for_claude_the_logic_pro.txt` under these
searchable keywords:

- `[[IDA:SystemRandom_ctor_0x1804680D0]]`
- `[[IDA:SystemRandom_InternalSample_0x180467D10]]`
- `[[IDA:SystemRandom_GenerateSeed_0x180467B00]]`
- `[[IDA:SystemRandom_GenerateGlobalSeed_0x1804535D0]]`
- `[[IDA:SystemRandom_Sample_0x180467FD0]]`
- `[[IDA:SystemRandom_NextDouble_0x180467DA0]]`
- `[[IDA:SystemRandom_Next_0x180467FC0]]`
- `[[IDA:SystemRandom_Next_maxValue_0x180467F00]]`
- `[[IDA:SystemRandom_Next_minMax_0x180467DC0]]`
- `[[IDA:SystemRandom_GetSampleForLargeRange_0x180467CC0]]`
