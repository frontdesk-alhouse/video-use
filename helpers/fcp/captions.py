"""Caption cues for the FCP path, with the SAME chunking as the ffmpeg path.

`render.chunk_words` is imported from helpers/render.py (not copied) so the
cue boundaries can never drift between master.srt and what lands in Final Cut.
Two renderings exist:

  native_caption_xml  → <caption> element (FCP caption lane, iTT role);
                        editable in FCP's caption editor, exportable as SRT.
  title_xml           → <title> Basic Title (video lane); for the bold social look.

Times inside these elements are in the PARENT CLIP'S local time (= source
time), because anchored items are scheduled on the parent's `start`-based
timeline. The output-timeline times are kept alongside for plan.json / parity.
"""
from __future__ import annotations

import importlib.util
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from xml.sax.saxutils import escape, quoteattr

HELPERS = Path(__file__).resolve().parent.parent


def _load_render():
    spec = importlib.util.spec_from_file_location("vu_render", HELPERS / "render.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("vu_render", mod)
    spec.loader.exec_module(mod)
    return mod


_render = None


def render_mod():
    global _render
    if _render is None:
        _render = _load_render()
    return _render


@dataclass
class Cue:
    text: str
    src_start: float   # seconds, source time (unsnapped)
    src_end: float
    out_start: float   # seconds, output timeline
    out_end: float


def cue_text(chunk: list[dict]) -> str:
    """Same normalisation as render.build_master_srt: collapse spaces, strip ,;: and UPPERCASE."""
    text = " ".join((w.get("text") or "").strip() for w in chunk)
    text = re.sub(r"\s+", " ", text).strip()
    return text.rstrip(",;:").upper()


def cues_for_range(transcript: dict, seg_start: float, seg_end: float, seg_offset: float) -> list[Cue]:
    r = render_mod()
    words = r._words_in_range(transcript, seg_start, seg_end)
    cues: list[Cue] = []
    for chunk in r.chunk_words(words):
        local_start = max(seg_start, chunk[0].get("start", seg_start))
        local_end = min(seg_end, chunk[-1].get("end", seg_end))
        out_start = max(0.0, local_start - seg_start) + seg_offset
        out_end = max(0.0, local_end - seg_start) + seg_offset
        if out_end <= out_start:            # mirror render.py's 0.4 s floor
            out_end = out_start + 0.4
            local_end = local_start + 0.4
        cues.append(Cue(cue_text(chunk), local_start, local_end, out_start, out_end))
    return cues


# -- XML renderings ----------------------------------------------------------

DEFAULT_CAPTION_STYLE = {
    "font": "Helvetica", "fontSize": "28", "bold": "1",
    "fontColor": "1 1 1 1", "backgroundColor": "0 0 0 0",
}

DEFAULT_TITLE_STYLE = {   # karaoke look, static (no per-word alpha)
    "font": "Helvetica", "fontSize": "54", "fontFace": "Bold",
    "fontColor": "1 1 1 1", "strokeColor": "0 0 0 0.5", "strokeWidth": "-4",
    "shadowColor": "0 0 0 0.2", "shadowOffset": "1 315", "shadowBlurRadius": "6",
    "alignment": "center",
}
# adjust-transform Y in % of frame height from centre. Upstream's SUB_FORCE_STYLE uses
# MarginV=90 of PlayResY=288 ≈ 31 % up from the bottom, clear of TikTok/Reels/Shorts UI
# on vertical video: 50 − 31 = 19 below centre.
TITLE_POSITION_Y = -19.0
BASIC_TITLE_UID = ".../Titles.localized/Bumper:Opener.localized/Basic Title.localized/Basic Title.moti"


def _style_attrs(style: dict) -> str:
    return " ".join(f"{k}={quoteattr(str(v))}" for k, v in style.items())


def native_caption_xml(text: str, offset: str, duration: str, style_id: str,
                       role: str, lane: int = 1, style: dict | None = None) -> str:
    st = _style_attrs(style or DEFAULT_CAPTION_STYLE)
    return (
        f'<caption lane="{lane}" offset="{offset}" name={quoteattr(text)} duration="{duration}" role={quoteattr(role)}>'
        f'<text placement="bottom"><text-style ref="{style_id}">{escape(text)}</text-style></text>'
        f'<text-style-def id="{style_id}"><text-style {st}/></text-style-def>'
        f'</caption>'
    )


def title_style(font: str | None = None, size: float | None = None, color: str | None = None,
                base: dict | None = None) -> dict:
    """DEFAULT_TITLE_STYLE (or `base`) with font / fontSize / fontColor overridden."""
    st = dict(base or DEFAULT_TITLE_STYLE)
    if font:
        st["font"] = font
    if size is not None:
        st["fontSize"] = f"{float(size):g}"
    if color:
        st["fontColor"] = color
    return st


def title_xml(text: str, offset: str, duration: str, style_id: str, effect_ref: str,
              lane: int = 2, position_y: float = TITLE_POSITION_Y, style: dict | None = None,
              font: str | None = None, size: float | None = None, color: str | None = None) -> str:
    st = _style_attrs(title_style(font, size, color, base=style))
    # DTD order for <title>: param*, text*, text-style-def*, note?, intrinsic video adjusts, anchors, markers
    return (
        f'<title ref="{effect_ref}" lane="{lane}" offset="{offset}" name={quoteattr(text)} start="3600s" duration="{duration}">'
        f'<text><text-style ref="{style_id}">{escape(text)}</text-style></text>'
        f'<text-style-def id="{style_id}"><text-style {st}/></text-style-def>'
        f'<adjust-transform position="0 {float(position_y):g}"/>'
        f'</title>'
    )
