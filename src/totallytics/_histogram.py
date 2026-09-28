from __future__ import annotations

import math
import struct

GROWTH = 1.08
MAX_BUCKET = 250

_DOUBLE = struct.Struct("<d")
_BITS = struct.Struct("<Q")

_LN2_HI = 6.93147180369123816490e-01
_LN2_LO = 1.90821492927058770002e-10
_LG1 = 6.666666666666735130e-01
_LG2 = 3.999999999940941908e-01
_LG3 = 2.857142874366239149e-01
_LG4 = 2.222219843214978396e-01
_LG5 = 1.818357216161805012e-01
_LG6 = 1.531383769920937332e-01
_LG7 = 1.479819860511658591e-01

# Libms disagree by an ulp, so positions this close to a bucket edge use V8's exact log
_EDGE = 1e-9


def _fdlibm_log(x: float) -> float:
    """fdlibm's log for normal x > 0, bit-identical to V8's Math.log (and so to the JS SDK and the server)."""
    bits = _BITS.unpack(_DOUBLE.pack(x))[0]
    high = bits >> 32
    k = (high >> 20) - 1023
    high &= 0x000FFFFF
    i = (high + 0x95F64) & 0x100000
    x = _DOUBLE.unpack(_BITS.pack(((high | (i ^ 0x3FF00000)) << 32) | (bits & 0xFFFFFFFF)))[0]
    k += i >> 20
    f = x - 1.0
    dk = float(k)

    if (0x000FFFFF & (2 + high)) < 3:
        if f == 0.0:
            return dk * _LN2_HI + dk * _LN2_LO if k else 0.0
        r = f * f * (0.5 - 0.33333333333333333 * f)
        return dk * _LN2_HI - ((r - dk * _LN2_LO) - f) if k else f - r

    s = f / (2.0 + f)
    z = s * s
    w = z * z
    t1 = w * (_LG2 + w * (_LG4 + w * _LG6))
    t2 = z * (_LG1 + w * (_LG3 + w * (_LG5 + w * _LG7)))
    r = t2 + t1

    if (high - 0x6147A) | (0x6B851 - high) > 0:
        half_square = 0.5 * f * f
        if k == 0:
            return f - (half_square - s * (half_square + r))
        return dk * _LN2_HI - ((half_square - (s * (half_square + r) + dk * _LN2_LO)) - f)
    if k == 0:
        return f - s * (f - r)
    return dk * _LN2_HI - ((s * (f - r) - dk * _LN2_LO) - f)


_LOG_GROWTH = _fdlibm_log(GROWTH)


def bucket(duration_ms: float) -> int:
    """Log-scaled latency bucket: 0 for <= 1 ms, then ceil(log_1.08(ms)), capped at 250."""
    if not duration_ms > 1:
        return 0

    position = math.log(duration_ms) / _LOG_GROWTH
    if position >= MAX_BUCKET:
        return MAX_BUCKET
    if abs(position - round(position)) < _EDGE:
        position = _fdlibm_log(duration_ms) / _LOG_GROWTH
    return min(math.ceil(position), MAX_BUCKET)
