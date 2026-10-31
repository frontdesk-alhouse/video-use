"""ffprobe → the facts FCPXML needs about a source file.

One ffprobe call per file (streams + format). Mirrors the policies upstream
`render.py` already uses: average frame rate first, display-matrix rotation
decides portrait, PQ/HLG transfer means HDR.
"""
from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path

HDR_TRANSFERS = {"smpte2084": "PQ", "arib-std-b67": "HLG"}

# FCPXML colorSpace strings: "<primaries>-<transfer>-<matrix> (<name>)", ISO 23001-8 codes.
COLORSPACE_REC709 = "1-1-1 (Rec. 709)"
COLORSPACE_REC2020 = "9-1-9 (Rec. 2020)"
COLORSPACE_REC2020_PQ = "9-16-9 (Rec. 2020 PQ)"
COLORSPACE_REC2020_HLG = "9-18-9 (Rec. 2020 HLG)"


@dataclass
class SourceInfo:
    path: Path
    width: int = 0
    height: int = 0
    fps: str | None = None          # canonical "num/den"
    rotation: int = 0               # display-matrix rotation, degrees
    duration: float = 0.0           # container duration, seconds
    has_video: bool = False
    has_audio: bool = False
    audio_streams: int = 0
    audio_channels: int = 0
    audio_rate: int = 0
    color_primaries: str = ""
    color_transfer: str = ""
    color_matrix: str = ""
    make: str = ""                  # container tag com.apple.quicktime.make
    timecode: str | None = None     # start timecode tag, e.g. "14:02:48:02" (';' = drop-frame)
    raw: dict = field(default_factory=dict, repr=False)

    @property
    def tc_start_frames(self) -> int:
        """Start timecode as a frame count on this file's own frame rate (0 when untagged).

        Final Cut addresses media on its timecode timeline: an asset whose file carries a
        start timecode must declare start=<tc>, and every clip/marker/anchored offset is
        tc + source time. Declaring start="0s" makes FCP drop the clips silently.
        """
        if not self.timecode or not self.fps:
            return 0
        return timecode_to_frames(self.timecode, Fraction(self.fps))

    # -- derived ------------------------------------------------------------
    @property
    def hdr(self) -> str | None:
        return HDR_TRANSFERS.get(self.color_transfer)

    @property
    def display_size(self) -> tuple[int, int]:
        if self.rotation % 360 in (90, 270):
            return self.height, self.width
        return self.width, self.height

    @property
    def portrait(self) -> bool:
        w, h = self.display_size
        return h > w

    @property
    def log_profile(self) -> str | None:
        """Camera log encoding FCP must be told about (Camera LUT), or None.

        iPhone Apple Log: make=Apple, BT.2020 primaries, and a transfer ffmpeg can't name
        (Apple Log has no CICP code). iPhone HLG declares arib-std-b67, so it is not caught.
        """
        if (self.make.lower() == "apple" and self.color_primaries == "bt2020"
                and self.color_transfer in ("", "unknown", "unspecified")):
            return "Apple_Log"
        return None

    @property
    def colorspace(self) -> str:
        if self.hdr == "PQ":
            return COLORSPACE_REC2020_PQ
        if self.hdr == "HLG":
            return COLORSPACE_REC2020_HLG
        if self.color_primaries == "bt2020":
            return COLORSPACE_REC2020
        return COLORSPACE_REC709

    @property
    def fps_fraction(self) -> Fraction | None:
        return Fraction(self.fps) if self.fps else None


# FCP's built-in log modes for asset@customLUTOverride, "<id> (<name>)". IDs read back from
# FCP 12.4 by import/export round-trip (2026-10-26); unknown IDs come back as "0 (None)".
LOG_MODES = {
    "ARRI_LogC_OriginalToneMap": 1, "ARRI_LogC_DefaultToneMap": 3, "ARRI_LogC4_WideGamut4": 4,
    "Canon_CanonLog_Rec709": 16, "Canon_CanonLog2_CinemaGamut": 17, "Canon_CanonLog3_CinemaGamut": 18,
    "Sony_SLog2_SGamut": 33, "Sony_SLog3_SGamut3Cine": 34, "Sony_SLog3_SGamut3Cine_v2": 35,
    "Sony_SLog3_SGamut3": 36, "BMD_Film": 48, "BMD_Film4K": 49, "BMD_FilmGen5_WideGamut": 50,
    "Panasonic_VLog_VGamut": 64, "DJI_DLog_DGamut": 80, "Nikon_NLog": 96,
    "Apple_Log": 112, "Apple_Log 2": 113, "Fujifilm_FLog": 128, "Fujifilm_FLog2": 129,
}


def lut_override(name: str) -> str:
    """'Apple_Log' → '112 (Apple_Log)'; an already-formatted value passes through."""
    if "(" in name:
        return name
    if name not in LOG_MODES:
        raise KeyError(f"unknown log mode {name!r}; known: {', '.join(LOG_MODES)}")
    return f"{LOG_MODES[name]} ({name})"


def timecode_to_frames(tc: str, fps: Fraction) -> int:
    """'HH:MM:SS:FF' (or ';' for drop-frame) → frame count at the nominal integer rate.

    Drop-frame skips frame numbers 0 and 1 of every minute except each tenth minute
    (SMPTE 12M), so the count is the non-drop count minus the skipped numbers.
    """
    import re
    parts = re.split(r"[:;.]", tc.strip())
    if len(parts) != 4:
        raise ValueError(f"bad timecode {tc!r}")
    h, m, s_, f = (int(x) for x in parts)
    nominal = int(round(float(fps)))
    drop = ";" in tc
    total_minutes = h * 60 + m
    frames = ((h * 3600 + m * 60 + s_) * nominal) + f
    if drop and nominal in (30, 60):
        per_min = 2 if nominal == 30 else 4
        frames -= per_min * (total_minutes - total_minutes // 10)
    return frames


def _canonical_fps(value: str | None) -> str | None:
    if not value or value == "0/0":
        return None
    try:
        rate = Fraction(value)
    except (ValueError, ZeroDivisionError):
        return None
    if rate <= 0:
        return None
    return f"{rate.numerator}/{rate.denominator}"


NOMINAL_FPS_TOLERANCE = Fraction(1, 2000)  # 0.05 %: iPhone jitter is ≤0.03 %, NTSC vs integer is 0.1 %


def _stream_fps(avg: str | None, nominal: str | None) -> str | None:
    """avg_frame_rate, unless it is a jittery reading of the nominal rate (iPhone: 3706800/123563
    for 30/1). FCP needs clip times on the nominal frame grid."""
    a, n = _canonical_fps(avg), _canonical_fps(nominal)
    if a and n and abs(Fraction(a) - Fraction(n)) <= Fraction(n) * NOMINAL_FPS_TOLERANCE:
        return n
    return a or n


def parse_ffprobe(path: Path, data: dict) -> SourceInfo:
    """Build a SourceInfo from ffprobe's JSON (separated for tests)."""
    info = SourceInfo(path=path, raw=data)
    fmt = data.get("format") or {}
    try:
        info.duration = float(fmt.get("duration") or 0.0)
    except (TypeError, ValueError):
        info.duration = 0.0
    ftags = fmt.get("tags") or {}
    info.make = ftags.get("com.apple.quicktime.make") or ftags.get("make") or ""
    fmt_tc = ftags.get("timecode")
    if fmt_tc:
        info.timecode = fmt_tc
    for s in data.get("streams") or []:
        kind = s.get("codec_type")
        if kind == "video" and not info.has_video:
            info.has_video = True
            info.width = int(s.get("width") or 0)
            info.height = int(s.get("height") or 0)
            info.fps = _stream_fps(s.get("avg_frame_rate"), s.get("r_frame_rate"))
            info.color_primaries = s.get("color_primaries") or ""
            info.color_transfer = s.get("color_transfer") or ""
            info.color_matrix = s.get("color_space") or ""
            for sd in s.get("side_data_list") or []:
                if sd.get("rotation") is not None:
                    try:
                        info.rotation = int(round(float(sd["rotation"]))) % 360
                    except (TypeError, ValueError):
                        pass
                    break
            if not info.duration:
                try:
                    info.duration = float(s.get("duration") or 0.0)
                except (TypeError, ValueError):
                    pass
        if not info.timecode:
            tc = (s.get("tags") or {}).get("timecode")
            if tc:
                info.timecode = tc
        if kind == "audio":
            info.audio_streams += 1
            if not info.has_audio:
                info.has_audio = True
                info.audio_channels = int(s.get("channels") or 0)
                info.audio_rate = int(s.get("sample_rate") or 0)
    return info


def probe(path: Path) -> SourceInfo:
    path = Path(path)
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)],
        capture_output=True, text=True, check=True,
    )
    return parse_ffprobe(path, json.loads(out.stdout))


def format_name(width: int, height: int, fps: Fraction) -> str:
    """Human label like FFVideoFormat1080p30 or FFVideoFormat3840x2160p2997 (informative only)."""
    if fps.denominator == 1:
        rate = str(fps.numerator)
    else:
        rate = f"{float(fps):.2f}".replace(".", "")
    if (width, height) in {(1920, 1080), (1080, 1920)}:
        return f"FFVideoFormat1080p{rate}"
    return f"FFVideoFormat{width}x{height}p{rate}"
