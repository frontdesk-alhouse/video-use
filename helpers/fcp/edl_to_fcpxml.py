#!/usr/bin/env python3
"""edl.json → FCPXML project for Final Cut Pro.

Reads the same EDL `render.py` renders with ffmpeg and writes:
  <edit_dir>/fcp/project.fcpxml   the project (assets, spine, fades, markers, overlays, captions)
  <edit_dir>/fcp/plan.json        every snapped time, for import reconciliation and verification
  <edit_dir>/master.srt           (unless --captions none) the ffmpeg-path SRT, kept as parity reference

What goes where (all anchored items are in the PARENT CLIP's local = source time):
  spine        one <asset-clip> per EDL range, offsets contiguous on the sequence frame grid
  fades        <adjust-volume><param name="amount"><fadeIn/><fadeOut/></param></adjust-volume>  [verify in FCP]
  markers      one <marker value="<beat>" note="<reason>"> at each clip's first frame
  overlays     connected <asset-clip lane="1"> anchored to the spine clip under their start
  captions     native <caption lane="1" role="iTT?captionFormat=ITT.<lang>"> (default)
               or Basic Title <title lane="2"> with --captions titles
  grade        <filter-video> Color Adjustments per clip, mapped from the EDL grade (preset,
               raw ffmpeg eq/curves/colorbalance, or "auto" per clip) — see grade_map.py

Usage:
    python helpers/fcp/edl_to_fcpxml.py <edit_dir>/edl.json [--fps 30] [--captions native|titles|none]
        [--fade-s 0.03] [--event NAME] [--project NAME] [--lang es] [--version 1.11]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime
from fractions import Fraction
from pathlib import Path
from xml.sax.saxutils import escape, quoteattr

sys.path.insert(0, str(Path(__file__).resolve().parent))
from captions import (BASIC_TITLE_UID, DEFAULT_TITLE_STYLE, Cue, cues_for_range,  # noqa: E402
                      native_caption_xml, render_mod, title_xml)
from grade_map import effect_resource_xml, ffmpeg_filter_to_params, filter_video_xml  # noqa: E402
from probe import SourceInfo, format_name, lut_override, probe  # noqa: E402
from timing import Timebase  # noqa: E402

FCPXML_VERSION = "1.11"
DEFAULT_FADE_S = 0.03          # Hard Rule 3: 30 ms at every cut (ffmpeg afade); ≥1 frame here
CAPTION_MODES = ("native", "titles", "both", "none")
DEFAULT_CAPTIONS = "both"   # editable native captions + Basic Title burned into the render


@dataclass
class BuildOptions:
    fps: str | None = None
    captions: str | None = None    # None → edl.fcp.captions or DEFAULT_CAPTIONS
    fade_s: float = DEFAULT_FADE_S
    event: str | None = None
    project: str | None = None
    lang: str | None = None
    version: str = FCPXML_VERSION
    probe_fn: object = probe   # injectable for tests
    still_fn: object = None    # (path, width, height, color) -> None; default: ffmpeg lavfi


@dataclass
class Resource:
    id: str
    xml: str


@dataclass
class Plan:
    fps: str = ""
    frame_duration: str = ""
    sequence: dict = field(default_factory=dict)
    event: str = ""
    project: str = ""
    clips: list[dict] = field(default_factory=list)
    overlays: list[dict] = field(default_factory=list)
    captions: list[dict] = field(default_factory=list)
    markers: list[dict] = field(default_factory=list)
    total_duration_s: float = 0.0
    edl_total_duration_s: float | None = None
    caption_mode: str = "native"
    fade_frames: int = 0
    grade: dict = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    edl_clamped: dict = field(default_factory=dict, repr=False)


# -- helpers -------------------------------------------------------------------

def _resolve_source(edit_dir: Path, p: str) -> Path:
    path = Path(p).expanduser()
    if not path.is_absolute():
        # EDL paths are either absolute or relative to the videos dir (edit_dir's parent)
        path = (edit_dir.parent / path)
    return path.resolve()


# FCP 12.4 rewrites some bare language codes on import (ITT.es → ITT.en, ITT.fr → ITT.en)
# but keeps language-region tags (ITT.es-MX, ITT.es-419). Verified 2026-10-26.
DEFAULT_REGION = {"es": "es-MX", "en": "en-US", "fr": "fr-FR", "pt": "pt-BR", "de": "de-DE",
                  "it": "it-IT", "ja": "ja-JP", "zh": "zh-CN", "ko": "ko-KR"}


def caption_locale(lang: str) -> str:
    """'es' → 'es-MX'; anything already carrying a region passes through."""
    lang = lang.replace("_", "-")
    if "-" in lang:
        return lang
    return DEFAULT_REGION.get(lang.lower(), lang)


def _language(edit_dir: Path, edl: dict, opt: BuildOptions) -> str:
    if opt.lang:
        return opt.lang
    fcp = edl.get("fcp") or {}
    if fcp.get("lang"):
        return fcp["lang"]
    env = os.environ.get("VIDEO_USE_STT_LANG")
    if env:
        return env
    for r in edl.get("ranges", []):
        if "source" not in r:
            continue
        tp = edit_dir / "transcripts" / f"{r['source']}.json"
        if tp.exists():
            try:
                code = json.loads(tp.read_text()).get("language_code")
                if code:
                    return str(code).split("-")[0]
            except (json.JSONDecodeError, OSError):
                pass
    return "en"


def _asset_xml(rid: str, fid: str, info: SourceInfo, tb_src: Timebase, lut: str | None = None) -> str:
    # floor: a clip may never claim more media than the file has, or FCP drops it silently
    dur_frames = max(1, tb_src.frames(info.duration, "floor"))
    attrs = [
        f'id="{rid}"', f"name={quoteattr(info.path.name)}", f'start="{tb_src.t(info.tc_start_frames)}"',
        f'duration="{tb_src.t(dur_frames)}"',
        f'hasVideo="{1 if info.has_video else 0}"', f'hasAudio="{1 if info.has_audio else 0}"',
        f'format="{fid}"',
    ]
    if info.has_video:
        attrs.append('videoSources="1"')
    if info.has_audio:
        attrs += [f'audioSources="{info.audio_streams or 1}"',
                  f'audioChannels="{info.audio_channels or 2}"',
                  f'audioRate="{info.audio_rate or 48000}"']
    if lut:
        attrs.append(f"customLUTOverride={quoteattr(lut)}")
    src = info.path.as_uri()
    return f'<asset {" ".join(attrs)}><media-rep kind="original-media" src={quoteattr(src)}/></asset>'


def _format_xml(fid: str, width: int, height: int, tb: Timebase, colorspace: str) -> str:
    return (f'<format id="{fid}" name={quoteattr(format_name(width, height, tb.fps))} '
            f'frameDuration="{tb.frame_duration}" width="{width}" height="{height}" '
            f'colorSpace={quoteattr(colorspace)}/>')


def _camera_lut(fcp_opts: dict, name: str, info: SourceInfo) -> str | None:
    """edl.fcp.camera_lut: "auto" (default: detected log profile), "none", a log-mode name,
    or a per-source dict {"<source>": "Apple_Log" | "none"}."""
    setting = fcp_opts.get("camera_lut", "auto")
    if isinstance(setting, dict):
        setting = setting.get(name, "auto")
    if setting in (None, "none", False):
        return None
    if setting == "auto":
        return lut_override(info.log_profile) if info.log_profile else None
    return lut_override(str(setting))


def _load_grade():
    import importlib.util
    spec = importlib.util.spec_from_file_location("vu_grade", Path(__file__).resolve().parent.parent / "grade.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _range_kind(r: dict) -> str:
    return render_mod().range_kind(r)


def _range_duration(r: dict) -> float:
    return render_mod().range_duration(r)


def _rotation(fcp_opts: dict, name: str | None) -> float:
    """edl.fcp.transform = {"<source>": {"rotation": 180}} → degrees (0 when unset)."""
    if not name:
        return 0.0
    t = (fcp_opts.get("transform") or {}).get(name) or {}
    return float(t.get("rotation", 0) or 0)


def _transform_xml(rotation: float) -> str:
    # DTD: intrinsic video params (adjust-transform) come BEFORE adjust-volume
    return f'<adjust-transform rotation="{rotation:g}"/>' if rotation else ""


MUTE_XML = '<adjust-volume amount="-96dB"/>'

# Still images: FCP's own exports give them a rate-less format and an hour-based start,
# like generators and titles. sRGB is what a PNG is.
STILL_FORMAT_NAME = "FFVideoFormatRateUndefined"
STILL_COLORSPACE = "1-13-1 (sRGB IEC61966-2.1)"
STILL_START_S = 3600          # local start of gaps, stills and titles
CARD_FONT = "Herculanum"      # Avatar's title face; ships with macOS, has Í/Á/É/Ó/Ú/Ñ
CARD_LINE_SIZE = 90.0
CARD_LINE_SPACING = 9.0       # % of frame height between consecutive card lines


def _ffmpeg_still(path: Path, width: int, height: int, color: str) -> None:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("ffmpeg not found: needed to draw the card background PNG")
    path.parent.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run([ffmpeg, "-y", "-loglevel", "error", "-f", "lavfi",
                           "-i", f"color=c={color}:s={width}x{height}", "-frames:v", "1", str(path)],
                          capture_output=True, text=True)
    if proc.returncode != 0 or not path.exists():
        raise RuntimeError(f"ffmpeg could not draw {path.name}: {proc.stderr.strip()}")


def _card_text_color(background: str) -> str:
    return "1 1 1 1" if background.lower() in ("black", "#000000", "0x000000") else "0 0 0 1"


def _card_style(font: str, size: float, color: str) -> dict:
    """Flat title look for a card: no stroke, no shadow (those are for text over footage)."""
    base = {k: v for k, v in DEFAULT_TITLE_STYLE.items()
            if not k.startswith(("stroke", "shadow")) and k != "fontFace"}
    return {**base, "font": font, "fontSize": f"{float(size):g}", "fontColor": color}


def _card_lines(card: dict) -> list[dict]:
    """Normalise card lines; lines without position_y are spread around the centre."""
    lines = [ln if isinstance(ln, dict) else {"text": str(ln)} for ln in (card.get("lines") or [])]
    if not lines:
        raise ValueError('card needs at least one line: {"card": {"lines": [{"text": ...}]}}')
    n = len(lines)
    out = []
    for i, ln in enumerate(lines):
        if not str(ln.get("text", "")).strip():
            raise ValueError(f"card line {i} has no text")
        default_y = ((n - 1) / 2 - i) * CARD_LINE_SPACING
        out.append({**ln, "position_y": float(ln.get("position_y", default_y))})
    return out


# -- builder -------------------------------------------------------------------

def build(edl: dict, edit_dir: Path, opt: BuildOptions | None = None) -> tuple[str, Plan]:
    opt = opt or BuildOptions()
    fcp_block = edl.get("fcp") or {}
    # the EDL's "fcp" block records the session's choices; CLI flags override it
    if opt.captions is None:
        mode = fcp_block.get("captions") if fcp_block.get("captions") in CAPTION_MODES else DEFAULT_CAPTIONS
        opt = BuildOptions(**{**opt.__dict__, "captions": mode})
    if opt.fade_s == DEFAULT_FADE_S and fcp_block.get("fade_s") is not None:
        opt = BuildOptions(**{**opt.__dict__, "fade_s": float(fcp_block["fade_s"])})
    if opt.captions not in CAPTION_MODES:
        raise ValueError(f"--captions must be one of {CAPTION_MODES}")
    edit_dir = Path(edit_dir).resolve()
    ranges = edl.get("ranges") or []
    if not ranges:
        raise ValueError("EDL has no ranges")
    fcp_opts = edl.get("fcp") or {}
    plan = Plan(caption_mode=opt.captions or "", edl_total_duration_s=edl.get("total_duration_s"))

    # 1. sources
    sources: dict[str, SourceInfo] = {}
    for name, p in (edl.get("sources") or {}).items():
        path = _resolve_source(edit_dir, p)
        if not path.exists():
            raise FileNotFoundError(f"source {name!r} not found: {path}")
        sources[name] = opt.probe_fn(path)
    for r in ranges:
        if _range_kind(r) == "media" and r["source"] not in sources:
            raise KeyError(f"range references unknown source {r['source']!r}")
    if not any(_range_kind(r) == "media" for r in ranges):
        raise ValueError("EDL needs at least one media range (gap/card alone have no format)")

    # 1b. clamp ranges to their media up front, so captions/SRT/plan all see the same cut
    ranges = [dict(r) for r in ranges]
    for i, r in enumerate(ranges):
        if _range_kind(r) != "media":
            if _range_duration(r) <= 0:
                raise ValueError(f"range {i} ({_range_kind(r)}) needs a positive duration: {r}")
            continue
        info = sources[r["source"]]
        if info.duration and float(r["end"]) > info.duration + 1e-3:
            plan.notes.append(f"range {i} end {float(r['end']):.3f} > {r['source']} duration "
                              f"{info.duration:.3f}; clamped to the media")
            r["end"] = info.duration
        if float(r["end"]) <= float(r["start"]):
            raise ValueError(f"range {i} has end <= start after clamping: {r}")
    edl = {**edl, "ranges": ranges}
    plan.edl_clamped = edl

    # 2. sequence timebase & format (same policy as render.py: explicit fps, else first source's)
    first = sources[next(r for r in ranges if _range_kind(r) == "media")["source"]]
    fps = opt.fps or fcp_opts.get("fps") or first.fps or "24"
    tb = Timebase.from_fps(fps)
    if first.portrait:
        seq_w, seq_h = 1080, 1920
    else:
        seq_w, seq_h = 1920, 1080
    seq_w = int(fcp_opts.get("width") or seq_w)
    seq_h = int(fcp_opts.get("height") or seq_h)
    seq_colorspace = fcp_opts.get("colorSpace") or "1-1-1 (Rec. 709)"
    if any(s.hdr for s in sources.values()) and seq_colorspace.startswith("1-1-1"):
        plan.notes.append("HDR source(s) placed in a Rec. 709 sequence: FCP tone-maps on import "
                          "(matches the ffmpeg path's tonemap). Override with edl.fcp.colorSpace.")
    plan.fps, plan.frame_duration = str(tb.fps), tb.frame_duration
    plan.sequence = {"width": seq_w, "height": seq_h, "colorSpace": seq_colorspace}

    # 3. resources: one format per distinct (w, h, fps, colorspace), one asset per source
    resources: list[Resource] = []
    format_ids: dict[tuple, str] = {}
    next_id = [1]

    def new_id() -> str:
        rid = f"r{next_id[0]}"
        next_id[0] += 1
        return rid

    def format_for(w: int, h: int, tbx: Timebase, cs: str) -> str:
        key = (w, h, tbx.frame_duration, cs)
        if key not in format_ids:
            fid = new_id()
            format_ids[key] = fid
            resources.append(Resource(fid, _format_xml(fid, w, h, tbx, cs)))
        return format_ids[key]

    seq_format = format_for(seq_w, seq_h, tb, seq_colorspace)
    asset_ids: dict[str, tuple[str, str, Timebase]] = {}   # name -> (asset id, format id, source tb)
    for name, info in sources.items():
        tb_src = Timebase.from_fps(info.fps) if info.fps else tb
        dw, dh = info.display_size          # rotated iPhone takes: display size, not coded size
        fid = format_for(dw or seq_w, dh or seq_h, tb_src, info.colorspace)
        aid = new_id()
        lut = _camera_lut(fcp_opts, name, info)
        if lut:
            plan.notes.append(f"{name}: Camera LUT {lut}")
        resources.append(Resource(aid, _asset_xml(aid, fid, info, tb_src, lut)))
        asset_ids[name] = (aid, fid, tb_src)

    # overlays: a separate file, or a cut of an EDL source (B-roll from a take)
    overlay_assets: list[tuple[dict, str, str, SourceInfo, Timebase, str]] = []
    for ov in edl.get("overlays") or []:
        if ov.get("source"):
            if ov["source"] not in sources:
                raise KeyError(f"overlay references unknown source {ov['source']!r}")
            aid, fid, tb_ov = asset_ids[ov["source"]]
            info = sources[ov["source"]]
            label = ov["source"]
        else:
            path = _resolve_source(edit_dir, ov["file"])
            if not path.exists():
                raise FileNotFoundError(f"overlay not found: {path}")
            info = opt.probe_fn(path)
            tb_ov = Timebase.from_fps(info.fps) if info.fps else tb
            fid = format_for(info.width or seq_w, info.height or seq_h, tb_ov, info.colorspace)
            aid = new_id()
            resources.append(Resource(aid, _asset_xml(aid, fid, info, tb_ov)))
            label = Path(ov["file"]).stem
        overlay_assets.append((ov, aid, fid, info, tb_ov, label))

    # card backgrounds: one still PNG per background colour
    still_fn = opt.still_fn or _ffmpeg_still
    still_assets: dict[str, str] = {}
    for r in ranges:
        if _range_kind(r) != "card":
            continue
        bg = str((r["card"] or {}).get("background", "white"))
        if bg in still_assets:
            continue
        slug = re.sub(r"[^A-Za-z0-9]+", "", bg) or "bg"
        png = edit_dir / "fcp" / f"card_{slug}_{seq_w}x{seq_h}.png"
        still_fn(png, seq_w, seq_h, bg)
        sfid = new_id()
        resources.append(Resource(sfid, f'<format id="{sfid}" name="{STILL_FORMAT_NAME}" width="{seq_w}" '
                                        f'height="{seq_h}" colorSpace={quoteattr(STILL_COLORSPACE)}/>'))
        said = new_id()
        resources.append(Resource(said, f'<asset id="{said}" name={quoteattr(png.name)} start="0s" duration="0s" '
                                        f'hasVideo="1" format="{sfid}" videoSources="1">'
                                        f'<media-rep kind="original-media" src={quoteattr(png.as_uri())}/></asset>'))
        still_assets[bg] = said

    title_effect_id = None
    if opt.captions in ("titles", "both") or still_assets:
        title_effect_id = new_id()
        resources.append(Resource(title_effect_id,
                                  f'<effect id="{title_effect_id}" name="Basic Title" uid={quoteattr(BASIC_TITLE_UID)}/>'))

    # 3b. grade → Color Adjustments params (per clip when "auto")
    grade = edl.get("grade")
    grade_mode = (fcp_opts.get("grade_mode") or "xml")
    grade_effect_id = None
    grade_params: dict[str, float] = {}
    grade_is_auto = False
    if grade and grade != "none" and grade_mode != "none":
        gmod = _load_grade()
        if grade == "auto":
            grade_is_auto = True
        else:
            try:
                flt = gmod.get_preset(grade)
            except KeyError:
                flt = str(grade)            # raw ffmpeg filter
            grade_params, unmapped = ffmpeg_filter_to_params(flt)
            if unmapped:
                plan.notes.append(f"grade: ffmpeg filters not mapped to Color Adjustments: {unmapped}")
        if grade_is_auto or grade_params:
            grade_effect_id = new_id()
            resources.append(Resource(grade_effect_id, effect_resource_xml(grade_effect_id)))
    plan.grade = {"edl": grade, "mode": grade_mode, "params": grade_params, "auto": grade_is_auto}

    # 4. spine
    fade_frames = max(1, tb.frames(opt.fade_s, "round")) if opt.fade_s > 0 else 0
    plan.fade_frames = fade_frames
    lang = _language(edit_dir, edl, opt)
    caption_role = f"iTT?captionFormat=ITT.{caption_locale(lang)}"
    transcripts: dict[str, dict | None] = {}
    style_n = [0]
    spine: list[str] = []
    cursor = 0                       # output frames
    seg_offset_s = 0.0               # unsnapped output seconds, for cue parity with render.py
    clip_windows: list[tuple[int, int, int]] = []   # (clip index, out_start_frame, out_end_frame)

    still_base_f = tb.frames(STILL_START_S, "floor")   # local start of gaps / stills, on the sequence grid

    def anchored_overlays(i: int, out_start_f: int, out_end_f: int, local_at) -> list[str]:
        """Overlays whose start falls inside spine item i. `local_at(out_f)` → the parent's
        local time string at that output frame (anchored offsets live in parent time)."""
        parts: list[str] = []
        for k, (ov, ov_aid, ov_fid, ov_info, tb_ov, label) in enumerate(overlay_assets):
            ov_start_f = tb.frames(float(ov.get("start_in_output", 0.0)), "round")
            if not (out_start_f <= ov_start_f < out_end_f):
                continue
            in_s = float(ov.get("source_start", 0.0))
            in_f = tb_ov.frames(in_s, "floor")
            avail_s = (ov_info.duration - tb_ov.seconds(in_f)) if ov_info.duration else None
            if avail_s is not None and avail_s <= 0:
                raise ValueError(f"overlay {label!r} source_start {in_s} is past the end of its media")
            ov_dur = float(ov.get("duration") or avail_s or 0)
            if avail_s is not None and ov_dur > avail_s + 1e-6:
                plan.notes.append(f"overlay {label!r} duration {ov_dur:.3f}s clamped to the media "
                                  f"after source_start ({avail_s:.3f}s)")
                ov_dur = avail_s
            ov_dur_f = max(1, tb.frames(ov_dur, "floor" if avail_s is not None else "round"))
            rotation = float(ov["rotation"]) if "rotation" in ov else _rotation(fcp_opts, ov.get("source"))
            muted = bool(ov.get("mute", True)) and ov_info.has_audio
            inner = _transform_xml(rotation) + (MUTE_XML if muted else "")
            head = (f'<asset-clip ref="{ov_aid}" lane="1" name={quoteattr(label)} '
                    f'offset="{local_at(ov_start_f)}" start="{tb_ov.t(ov_info.tc_start_frames + in_f)}" '
                    f'duration="{tb.t(ov_dur_f)}" format="{ov_fid}" tcFormat="NDF"')
            parts.append(f"{head}>{inner}</asset-clip>" if inner else f"{head}/>")
            # an overlay may run past its parent clip: FCP keeps it connected to that clip
            spill_f = max(0, ov_start_f + ov_dur_f - out_end_f)
            plan.overlays.append({"file": ov.get("file") or ov.get("source"), "overlay_index": k,
                                  "source": ov.get("source"), "clip_index": i,
                                  "start_in_output_s": tb.seconds(ov_start_f),
                                  "duration_s": tb.seconds(ov_dur_f),
                                  "source_start_s": tb_ov.seconds(in_f),
                                  "rotation": rotation, "muted": muted,
                                  "offset_in_clip": local_at(ov_start_f),
                                  "past_parent_s": tb.seconds(spill_f)})
        return parts

    for i, r in enumerate(ranges):
        kind = _range_kind(r)
        if kind != "media":
            dur_f = max(1, tb.frames(_range_duration(r), "round"))
            out_start_f, out_end_f = cursor, cursor + dur_f
            clip_windows.append((i, out_start_f, out_end_f))
            beat = r.get("beat") or kind.upper()
            label = f"{beat} · {kind}"
            local_start = tb.t(still_base_f)

            def local_at(out_f: int, _s=out_start_f) -> str:
                return tb.t(still_base_f + out_f - _s)

            if kind == "gap":
                parts = [f'<gap name={quoteattr(label)} offset="{tb.t(cursor)}" start="{local_start}" '
                         f'duration="{tb.t(dur_f)}">']
            else:
                card = r["card"] or {}
                bg = str(card.get("background", "white"))
                parts = [f'<video ref="{still_assets[bg]}" name={quoteattr(label)} offset="{tb.t(cursor)}" '
                         f'start="{local_start}" duration="{tb.t(dur_f)}">']
            parts += anchored_overlays(i, out_start_f, out_end_f, local_at)
            if kind == "card":
                card_font = card.get("font") or CARD_FONT
                card_color = card.get("color") or _card_text_color(bg)
                for li, ln in enumerate(_card_lines(card)):
                    style_n[0] += 1
                    style = _card_style(ln.get("font") or card_font, float(ln.get("size", CARD_LINE_SIZE)),
                                        ln.get("color") or card_color)
                    # one lane per line: same-lane titles may not overlap
                    parts.append(title_xml(str(ln["text"]), local_start, tb.t(dur_f), f"cd{style_n[0]}",
                                           title_effect_id, lane=li + 1, position_y=ln["position_y"],
                                           style=style))
            note = r.get("reason") or ""
            parts.append(f'<marker start="{local_start}" duration="{tb.t(1)}" value={quoteattr(str(beat))}'
                         + (f' note={quoteattr(str(note))}' if note else "") + "/>")
            plan.markers.append({"clip_index": i, "out_s": tb.seconds(cursor), "value": str(beat)})
            parts.append("</gap>" if kind == "gap" else "</video>")
            spine.append("".join(parts))
            plan.clips.append({
                "index": i, "source": kind, "kind": kind, "beat": beat,
                "duration_s": tb.seconds(dur_f), "out_start_s": tb.seconds(cursor),
                "out_end_s": tb.seconds(cursor + dur_f), "frames": dur_f, "has_audio": False,
                **({"card_lines": [ln["text"] for ln in _card_lines(card)]} if kind == "card" else {}),
            })
            cursor += dur_f
            seg_offset_s += _range_duration(r)
            continue

        name = r["source"]
        aid, fid, tb_src = asset_ids[name]
        info = sources[name]
        start_s, end_s = float(r["start"]), float(r["end"])
        if end_s <= start_s:
            raise ValueError(f"range {i} has end <= start: {r}")
        src_start_f = tb_src.frames(start_s, "floor")            # on the SOURCE grid
        dur_f = max(1, tb.frames(end_s - start_s, "round"))      # on the SEQUENCE grid
        # never run past the media's last frame (FCP drops such clips silently)
        media_f = tb_src.frames(info.duration, "floor") if info.duration else None
        if media_f is not None:
            max_dur_f = int((media_f - src_start_f) * tb_src.frame / tb.frame)
            if max_dur_f < 1:
                raise ValueError(f"range {i} starts at/after the end of {name}")
            dur_f = min(dur_f, max_dur_f)
        tc0 = info.tc_start_frames          # media timeline base (start timecode)
        offset = tb.t(cursor)
        start = tb_src.t(tc0 + src_start_f)
        duration = tb.t(dur_f)
        label = f"{r.get('beat', f'seg {i+1:02d}')} · {name}"
        attrs = [f'ref="{aid}"', f"name={quoteattr(label)}", f'offset="{offset}"',
                 f'start="{start}"', f'duration="{duration}"', f'format="{fid}"', 'tcFormat="NDF"']
        if info.has_audio:
            attrs.append('audioRole="dialogue"')
        parts = [f'<asset-clip {" ".join(attrs)}>']

        # intrinsic video params (DTD: first, before adjust-volume)
        rotation = _rotation(fcp_opts, name)
        if rotation:
            parts.append(_transform_xml(rotation))

        # intrinsic audio params (DTD: before anchored items)
        if info.has_audio and fade_frames:
            fd = tb.t(min(fade_frames, max(1, dur_f // 2)))
            parts.append(f'<adjust-volume amount="0dB"><param name="amount">'
                         f'<fadeIn type="easeIn" duration="{fd}"/><fadeOut type="easeOut" duration="{fd}"/>'
                         f'</param></adjust-volume>')

        out_start_f, out_end_f = cursor, cursor + dur_f
        clip_windows.append((i, out_start_f, out_end_f))
        src_start_snapped_s = tb_src.seconds(src_start_f)

        # anchored overlays whose start falls inside this clip
        def media_local_at(out_f: int, _s=out_start_f, _tb=tb_src, _f0=src_start_f, _tc=tc0) -> str:
            return _tb.t(_tc + _f0 + int((out_f - _s) * tb.frame / _tb.frame))

        parts += anchored_overlays(i, out_start_f, out_end_f, media_local_at)

        # anchored captions
        if opt.captions != "none":
            if name not in transcripts:
                tp = edit_dir / "transcripts" / f"{name}.json"
                transcripts[name] = json.loads(tp.read_text()) if tp.exists() else None
                if transcripts[name] is None:
                    plan.notes.append(f"no transcript for {name}: no captions on its clips")
            tr = transcripts[name]
            cues = cues_for_range(tr, start_s, end_s, seg_offset_s) if tr else []
            prev_end_f = -1
            for cue in cues:
                c_start_f = tb_src.frames(cue.src_start, "floor")
                c_end_f = max(c_start_f + 1, tb_src.frames(cue.src_end, "round"))
                if c_start_f <= prev_end_f:                 # same lane may not overlap by a frame
                    c_start_f = prev_end_f + 1
                    c_end_f = max(c_end_f, c_start_f + 1)
                c_start_f = max(c_start_f, src_start_f)
                clip_end_src_f = src_start_f + int(dur_f * tb.frame / tb_src.frame)
                c_end_f = min(c_end_f, clip_end_src_f)
                if c_end_f <= c_start_f:
                    continue
                prev_end_f = c_end_f
                style_n[0] += 1
                sid = f"ts{style_n[0]}"
                off, dur = tb_src.t(tc0 + c_start_f), tb_src.t(c_end_f - c_start_f)
                if opt.captions in ("native", "both"):
                    parts.append(native_caption_xml(cue.text, off, dur, sid, caption_role))
                if opt.captions in ("titles", "both"):
                    tsid = sid if opt.captions == "titles" else f"tt{style_n[0]}"
                    parts.append(title_xml(cue.text, off, dur, tsid, title_effect_id))
                plan.captions.append({
                    "clip_index": i, "text": cue.text,
                    "src_start_s": tb_src.seconds(c_start_f), "src_end_s": tb_src.seconds(c_end_f),
                    "out_start_s": tb.seconds(out_start_f + int((c_start_f - src_start_f) * tb_src.frame / tb.frame)),
                    "out_end_s": tb.seconds(out_start_f + int((c_end_f - src_start_f) * tb_src.frame / tb.frame)),
                    "srt_start_s": cue.out_start, "srt_end_s": cue.out_end,
                })

        # markers (DTD: after anchored items)
        beat = r.get("beat") or f"seg {i+1:02d}"
        note = r.get("reason") or r.get("quote") or ""
        parts.append(f'<marker start="{start}" duration="{tb_src.t(1)}" value={quoteattr(str(beat))}'
                     + (f' note={quoteattr(str(note))}' if note else "") + "/>")
        plan.markers.append({"clip_index": i, "out_s": tb.seconds(cursor), "value": str(beat)})

        # video filters (DTD: after markers)
        if grade_effect_id:
            params = grade_params
            if grade_is_auto:
                flt, _stats = _load_grade().auto_grade_for_clip(info.path, start_s, end_s - start_s)
                params, _un = ffmpeg_filter_to_params(flt)
            if params:
                parts.append(filter_video_xml(grade_effect_id, params, sdr=not info.hdr))
            plan.grade.setdefault("per_clip", []).append({"clip_index": i, "params": params})

        parts.append("</asset-clip>")
        spine.append("".join(parts))
        plan.clips.append({
            "index": i, "source": name, "beat": beat, "tc_start_s": tb_src.seconds(tc0),
            "edl_start_s": start_s, "edl_end_s": end_s,
            "src_start_s": src_start_snapped_s, "duration_s": tb.seconds(dur_f),
            "out_start_s": tb.seconds(cursor), "out_end_s": tb.seconds(cursor + dur_f),
            "frames": dur_f, "has_audio": info.has_audio, "kind": "media", "rotation": rotation,
        })
        cursor += dur_f
        seg_offset_s += end_s - start_s

    placed = {o["overlay_index"] for o in plan.overlays}
    for k, (ov, *_rest, label) in enumerate(overlay_assets):
        if k not in placed:
            plan.notes.append(f"overlay {label} at {float(ov.get('start_in_output', 0)):.3f}s "
                              f"falls outside the timeline; skipped")

    plan.total_duration_s = tb.seconds(cursor)
    if plan.edl_total_duration_s is not None and abs(plan.edl_total_duration_s - plan.total_duration_s) > 2 * float(tb.frame):
        plan.notes.append(f"EDL total_duration_s {plan.edl_total_duration_s} ≠ built {plan.total_duration_s:.3f}")

    # 5. names
    videos_name = edit_dir.parent.name or "video-use"
    plan.event = opt.event or fcp_opts.get("event") or f"video-use · {videos_name}"
    plan.project = (opt.project or fcp_opts.get("project")
                    or f"{videos_name} · {datetime.now().strftime('%Y-%m-%d %H:%M')}")

    xml = "\n".join([
        '<?xml version="1.0" encoding="UTF-8"?>',
        "<!DOCTYPE fcpxml>",
        f'<fcpxml version="{opt.version}">',
        "<resources>",
        *[res.xml for res in resources],
        "</resources>",
        "<library>",
        f"<event name={quoteattr(plan.event)}>",
        f"<project name={quoteattr(plan.project)}>",
        f'<sequence format="{seq_format}" duration="{tb.t(cursor)}" tcStart="0s" tcFormat="NDF" '
        f'audioLayout="stereo" audioRate="48k">',
        "<spine>",
        *spine,
        "</spine>",
        "</sequence>",
        "</project>",
        "</event>",
        "</library>",
        "</fcpxml>",
        "",
    ])
    return xml, plan


def write_outputs(edl: dict, edit_dir: Path, opt: BuildOptions) -> tuple[Path, Path]:
    xml, plan = build(edl, edit_dir, opt)
    out_dir = edit_dir / "fcp"
    out_dir.mkdir(parents=True, exist_ok=True)
    xml_path = out_dir / "project.fcpxml"
    plan_path = out_dir / "plan.json"
    xml_path.write_text(xml, encoding="utf-8")
    public = {k: v for k, v in plan.__dict__.items() if k != "edl_clamped"}
    plan_path.write_text(json.dumps(public, indent=2, ensure_ascii=False), encoding="utf-8")
    if opt.captions != "none":
        # always rebuilt from the SAME (clamped) EDL the XML was built from, so parity holds
        render_mod().build_master_srt(plan.edl_clamped or edl, edit_dir, edit_dir / "master.srt")
    return xml_path, plan_path


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("edl", type=Path, help="path to edit/edl.json")
    ap.add_argument("--edit-dir", type=Path, default=None, help="default: the EDL's directory")
    ap.add_argument("--fps", default=None, help="sequence frame rate, e.g. 30 or 30000/1001 (default: first source)")
    ap.add_argument("--captions", choices=CAPTION_MODES, default=None,
                    help=f"default: edl.fcp.captions, else {DEFAULT_CAPTIONS}")
    ap.add_argument("--fade-s", type=float, default=DEFAULT_FADE_S)
    ap.add_argument("--event", default=None)
    ap.add_argument("--project", default=None)
    ap.add_argument("--lang", default=None, help="caption language code for the iTT role (default: transcript's)")
    ap.add_argument("--version", default=FCPXML_VERSION)
    ap.add_argument("--validate", action="store_true", help="run validate.py on the result")
    a = ap.parse_args()

    edl = json.loads(a.edl.read_text())
    edit_dir = (a.edit_dir or a.edl.parent).resolve()
    opt = BuildOptions(fps=a.fps, captions=a.captions, fade_s=a.fade_s, event=a.event,
                       project=a.project, lang=a.lang, version=a.version)
    xml_path, plan_path = write_outputs(edl, edit_dir, opt)
    plan = json.loads(plan_path.read_text())
    print(f"FCPXML → {xml_path}  ({xml_path.stat().st_size/1024:.1f} KB)")
    print(f"plan   → {plan_path}  clips={len(plan['clips'])} overlays={len(plan['overlays'])} "
          f"captions={len(plan['captions'])} total={plan['total_duration_s']:.3f}s @ {plan['fps']} fps")
    for n in plan["notes"]:
        print(f"  note: {n}")
    if a.validate:
        from validate import validate
        errs = validate(xml_path)
        if errs:
            print(f"validation: {len(errs)} problem(s)")
            for e in errs:
                print(f"  - {e}")
            return 1
        print("validation: OK (DTD + semantic)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
