"""
net_random.py

Bit-exact Python port of the legacy .NET "Net5Compat" System.Random
(classic Knuth subtractive lagged-Fibonacci generator), reverse-engineered
from Granny Legacy's GameAssembly.dll (IL2CPP build). See
`re_system_random.md` in this repository for the full RE report that this
port is derived from.

Confirmed algorithm shape (matches legacy .NET Core `Random` exactly):
    - int[56] _seedArray, _inext, _inextp
    - MSEED = 161803398, MBIG = 2147483647 (int.MaxValue)
    - seed normalization uses the .NET-Core-style fix:
          subtraction = int.MaxValue           if seed == int.MinValue
                       = abs(seed)              otherwise
    - _inext = 0, _inextp = 21 after construction, with 4 warm-up passes
    - InternalSample()'s index advance wraps via "++x; if (x >= 56) x = 1"
      (NOT modulo)
    - Sample() = InternalSample() * (1.0 / MBIG)
    - Next(max) = (int)(Sample() * max)                  [trunc toward zero]
    - Next(min, max):
          range = (long) max - min
          if range <= int.MaxValue:
              result = (int)(Sample() * (double) range)
          else:
              result = (int)(GetSampleForLargeRange() * (double) range)
          return min + result

All arithmetic below that C# performs as 32-bit signed integers is
explicitly wrapped to int32 semantics via `_wrap32`, since Python integers
are arbitrary-precision and would otherwise silently diverge from C#'s
wraparound-on-overflow behavior.
"""

import math

MBIG = 2147483647          # int.MaxValue
MSEED = 161803398          # Knuth's magic seed constant
_INT32_MIN = -2147483648
_INT32_MAX = 2147483647


def _wrap32(x: int) -> int:
    """
    Reinterpret an arbitrary-precision Python int as a signed 32-bit
    integer, exactly matching C#'s `unchecked` 32-bit wraparound semantics
    (two's-complement, mod 2**32).
    """
    x &= 0xFFFFFFFF
    if x >= 0x80000000:
        x -= 0x100000000
    return x


class NetRandom:
    """Bit-exact port of the seeded legacy .NET `System.Random(int seed)`."""

    __slots__ = ("_seed_array", "_inext", "_inextp")

    def __init__(self, seed: int):
        # Normalize the incoming Python int to C# `int` (32-bit signed)
        # domain first, in case caller passes an out-of-int32-range value.
        seed = _wrap32(seed)

        # --- Confirmed .NET-Core-style seed normalization fix ---
        # (Seed == int.MinValue) ? int.MaxValue : abs(Seed)
        if seed == _INT32_MIN:
            subtraction = _INT32_MAX
        else:
            subtraction = abs(seed)

        mj = _wrap32(MSEED - subtraction)

        seed_array = [0] * 56
        seed_array[55] = mj
        mk = 1
        for i in range(1, 55):
            ii = (21 * i) % 55
            seed_array[ii] = mk
            mk = _wrap32(mj - mk)
            if mk < 0:
                mk += MBIG
            mj = seed_array[ii]

        for _k in range(1, 5):  # exactly 4 warm-up passes
            for i in range(1, 56):
                v = _wrap32(seed_array[i] - seed_array[1 + (i + 30) % 55])
                if v < 0:
                    v += MBIG
                seed_array[i] = v

        self._seed_array = seed_array
        self._inext = 0
        self._inextp = 21

    def internal_sample(self) -> int:
        """Core PRNG step; returns an int in [0, MBIG - 1]."""
        seed_array = self._seed_array

        loc_inext = self._inext + 1
        if loc_inext >= 56:
            loc_inext = 1
        loc_inextp = self._inextp + 1
        if loc_inextp >= 56:
            loc_inextp = 1

        # Values already stored in seed_array are always within
        # [0, MBIG - 1], so this plain subtraction can never overflow
        # int32 range -- matches the C# source exactly (no wrap needed).
        ret_val = seed_array[loc_inext] - seed_array[loc_inextp]
        if ret_val == MBIG:
            ret_val -= 1
        if ret_val < 0:
            ret_val += MBIG

        seed_array[loc_inext] = ret_val
        self._inext = loc_inext
        self._inextp = loc_inextp
        return ret_val

    def sample(self) -> float:
        """Returns a double in [0.0, 1.0)."""
        return self.internal_sample() * (1.0 / MBIG)

    def next(self) -> int:
        """Equivalent to C# `Random.Next()`."""
        return self.internal_sample()

    def next_int(self, max_value: int) -> int:
        """Equivalent to C# `Random.Next(int maxValue)`."""
        if max_value < 0:
            raise ValueError("maxValue must be non-negative")
        # Sample() in [0, 1), max_value >= 0 => product always >= 0,
        # so trunc-toward-zero == floor here; math.trunc used for clarity
        # and to exactly mirror C#'s (int) cast semantics.
        return math.trunc(self.sample() * max_value)

    def _get_sample_for_large_range(self) -> float:
        a = self.internal_sample()
        odd_second = (self.internal_sample() & 1) != 0
        v = a if odd_second else -a
        return (v + 2147483646.0) / 4294967293.0

    def next_range(self, min_value: int, max_value: int) -> int:
        """Equivalent to C# `Random.Next(int minValue, int maxValue)`."""
        if min_value > max_value:
            raise ValueError("minValue must be <= maxValue")
        # Python ints are arbitrary precision, so this matches C#'s
        # `(long)maxValue - minValue` widening without needing an
        # explicit 64-bit wrap (the difference of two int32s always
        # fits in an int64/Python int exactly).
        rng = max_value - min_value
        if rng <= MBIG:
            result = math.trunc(self.sample() * float(rng))
        else:
            result = math.trunc(self._get_sample_for_large_range() * float(rng))
        return min_value + result

    def next_double(self) -> float:
        """Equivalent to C# `Random.NextDouble()`."""
        return self.sample()
