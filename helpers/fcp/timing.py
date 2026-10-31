"""Frame-grid arithmetic for FCPXML.

FCPXML writes every time as a rational "N/Ds". Final Cut rejects values that do
not sit on the sequence frame grid, so all builder math happens in integer
frames and only converts to seconds at the boundary.

    tb = Timebase.from_fps("30000/1001")
    tb.frame_duration          -> "1001/30000s"
    tb.frames(2.42)            -> 72          (floor)
    tb.t(72)                   -> "3003/1250s"   (= 72072/30000, reduced)
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from fractions import Fraction

# fps strings ffprobe emits -> exact rational frame duration
_COMMON = {
    "24": Fraction(1, 24), "25": Fraction(1, 25), "30": Fraction(1, 30),
    "50": Fraction(1, 50), "60": Fraction(1, 60),
    "24000/1001": Fraction(1001, 24000), "30000/1001": Fraction(1001, 30000),
    "60000/1001": Fraction(1001, 60000),
}


@dataclass(frozen=True)
class Timebase:
    frame: Fraction  # duration of one frame in seconds

    @classmethod
    def from_fps(cls, fps: str | float | Fraction) -> "Timebase":
        if isinstance(fps, str):
            key = fps.strip()
            if key in _COMMON:
                return cls(_COMMON[key])
            fr = Fraction(key)
        else:
            fr = Fraction(fps).limit_denominator(100000)
        if fr <= 0:
            raise ValueError(f"invalid fps {fps!r}")
        return cls(1 / fr)

    @property
    def fps(self) -> Fraction:
        return 1 / self.frame

    @property
    def frame_duration(self) -> str:
        return rational(self.frame)

    def frames(self, seconds: float, mode: str = "floor") -> int:
        """Seconds -> integer frames. mode: floor (starts), round (durations), ceil."""
        x = Fraction(seconds).limit_denominator(1_000_000) / self.frame
        if mode == "floor":
            return math.floor(x + Fraction(1, 1000))  # tolerate float noise just below a frame
        if mode == "ceil":
            return math.ceil(x - Fraction(1, 1000))
        return int(round(x))

    def seconds(self, frames: int) -> float:
        return float(frames * self.frame)

    def t(self, frames: int) -> str:
        """Integer frames -> FCPXML rational string on this grid."""
        return rational(frames * self.frame)

    def snap_range(self, start_s: float, end_s: float) -> tuple[int, int]:
        """Source range in seconds -> (start_frame, duration_frames), min 1 frame."""
        start = self.frames(start_s, "floor")
        end = self.frames(end_s, "round")
        return start, max(1, end - start)


def rational(x: Fraction) -> str:
    x = Fraction(x)
    if x == 0:
        return "0s"
    if x.denominator == 1:
        return f"{x.numerator}s"
    return f"{x.numerator}/{x.denominator}s"


def parse_rational(s: str) -> Fraction:
    """'7260/3000s' -> Fraction(121, 50); '0s' -> 0."""
    s = s.strip()
    if not s.endswith("s"):
        raise ValueError(f"not an FCPXML time: {s!r}")
    body = s[:-1]
    if "/" in body:
        n, d = body.split("/", 1)
        return Fraction(int(n), int(d))
    return Fraction(body)
