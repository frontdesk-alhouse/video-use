"""Shared fixture for the FCP builder tests: a tiny EDL with fake (empty) media.

Two sources (A: 1920x1080 @ 30 fps, B: 3840x2160 @ 30000/1001), three ranges
(A, B, A), one overlay at 0.5 s for 3.0 s, Scribe-shaped transcripts. Media
files are empty placeholders; `fake_probe` answers for them by file name.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

HELPERS = Path(__file__).parents[1] / "helpers"
for p in (HELPERS, HELPERS / "fcp"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from probe import SourceInfo  # noqa: E402

A_NAME, B_NAME, OV_NAME = "take a.mp4", "take b.mov", "broll one.mp4"

INFO = {
    A_NAME: dict(width=1920, height=1080, fps="30/1", duration=10.0, has_video=True,
                 has_audio=True, audio_streams=1, audio_channels=2, audio_rate=48000,
                 color_primaries="bt709", color_transfer="bt709", color_matrix="bt709"),
    B_NAME: dict(width=3840, height=2160, fps="30000/1001", duration=6.0, has_video=True,
                 has_audio=True, audio_streams=1, audio_channels=2, audio_rate=48000,
                 color_primaries="bt709", color_transfer="bt709", color_matrix="bt709"),
    OV_NAME: dict(width=1920, height=1080, fps="30/1", duration=5.0, has_video=True,
                  has_audio=False),
}


def fake_probe(path) -> SourceInfo:
    path = Path(path)
    return SourceInfo(path=path, **INFO[path.name])


def _words(script: list[tuple[str, float, float]]) -> list[dict]:
    out: list[dict] = []
    for i, (text, s, e) in enumerate(script):
        if i:
            prev_end = script[i - 1][2]
            if s > prev_end:
                out.append({"type": "spacing", "text": " ", "start": prev_end, "end": s})
        out.append({"type": "word", "text": text, "start": s, "end": e, "speaker_id": "speaker_0"})
    return out


A_SCRIPT = [
    ("Hello", 1.10, 1.40), ("there,", 1.45, 1.80), ("friends.", 1.85, 2.30),
    ("This", 2.40, 2.55), ("is", 2.58, 2.70), ("it.", 2.72, 2.95),
    ("Back", 5.10, 5.35), ("again", 5.40, 5.80), ("with", 5.90, 6.05), ("more", 6.10, 6.40),
    ("news;", 6.45, 6.80), ("bye.", 6.90, 7.15),
]
B_SCRIPT = [
    ("Second", 2.10, 2.50), ("source", 2.55, 2.95), ("talks", 3.00, 3.30),
    ("here.", 3.35, 3.70), ("Yes", 3.90, 4.10), ("indeed.", 4.15, 4.45),
]

RANGES = [
    {"source": "A", "start": 1.0, "end": 3.0, "beat": "HOOK", "reason": "opens strong"},
    {"source": "B", "start": 2.0, "end": 4.5, "beat": "PROBLEM", "quote": "Second source"},
    {"source": "A", "start": 5.0, "end": 7.2, "beat": "PAYOFF"},
]


def make_project(tmp: Path, grade: str | None = "warm_cinematic") -> tuple[dict, Path]:
    """Create <tmp>/my videos/{media, edit/transcripts}; return (edl, edit_dir)."""
    videos = Path(tmp) / "my videos"
    edit_dir = videos / "edit"
    (edit_dir / "transcripts").mkdir(parents=True)
    for name in (A_NAME, B_NAME, OV_NAME):
        (videos / name).touch()
    (edit_dir / "transcripts" / "A.json").write_text(
        json.dumps({"language_code": "en", "words": _words(A_SCRIPT)}))
    (edit_dir / "transcripts" / "B.json").write_text(
        json.dumps({"language_code": "en", "words": _words(B_SCRIPT)}))
    edl = {
        "sources": {"A": str(videos / A_NAME), "B": str(videos / B_NAME)},
        "ranges": [dict(r) for r in RANGES],
        "overlays": [{"file": str(videos / OV_NAME), "start_in_output": 0.5, "duration": 3.0}],
        "total_duration_s": sum(r["end"] - r["start"] for r in RANGES),
    }
    if grade is not None:
        edl["grade"] = grade
    return edl, edit_dir
