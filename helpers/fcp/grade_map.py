"""Map video-use grades (ffmpeg filter strings) to Final Cut's Color Adjustments effect.

Final Cut's Color Adjustments parameters, with the keys FCP writes in FCPXML
(taken from a real export):
    Black Point(1) Brightness(2) Exposure(3) Shadows(4) Highlights(7)
    Highlights Warmth(10) Highlights Tint(11) Midtones Warmth(12) Midtones Tint(13)
    Shadows Warmth(14) Shadows Tint(15) Saturation(16) Contrast(17)
All are percentages centred on 0.

The mapping from ffmpeg `eq`/`curves`/`colorbalance` is an APPROXIMATION
(different maths, same intent): contrast 1.12 → +12, saturation 0.88 → −12,
brightness −0.02 → −2, gamma 1.10 → brightness +5, an S-curve's 25 % / 75 %
points → Shadows / Highlights, colorbalance r−b per zone → Warmth, g → Tint.
Anything the parser does not understand is reported so the agent can decide.
"""
from __future__ import annotations

import re
from xml.sax.saxutils import quoteattr

COLOR_ADJUSTMENTS_UID = "FxPlug:7E2022A5-202B-4EEB-A311-AC2B585D01B0"
COLOR_ADJUSTMENTS_NAME = "Color Adjustments"
PARAM_KEYS = {
    "Black Point": 1, "Brightness": 2, "Exposure": 3, "Shadows": 4, "Highlights": 7,
    "Highlights Warmth": 10, "Highlights Tint": 11, "Midtones Warmth": 12, "Midtones Tint": 13,
    "Shadows Warmth": 14, "Shadows Tint": 15, "Saturation": 16, "Contrast": 17,
}
LIMIT = 100.0


def _clamp(x: float) -> float:
    return max(-LIMIT, min(LIMIT, x))


def _kv(body: str) -> dict[str, str]:
    out = {}
    for part in body.split(":"):
        if "=" in part:
            k, v = part.split("=", 1)
            out[k.strip()] = v.strip().strip("'\"")
    return out


def ffmpeg_filter_to_params(filter_str: str) -> tuple[dict[str, float], list[str]]:
    """Parse an ffmpeg -vf chain into Color Adjustments params. Returns (params, unmapped)."""
    params: dict[str, float] = {}
    unmapped: list[str] = []
    if not filter_str:
        return params, unmapped
    # split on commas that are not inside quotes
    chain = re.split(r",(?=(?:[^']*'[^']*')*[^']*$)", filter_str)
    for f in chain:
        f = f.strip()
        if not f:
            continue
        name, _, body = f.partition("=")
        name = name.strip()
        if name == "eq":
            kv = _kv(body)
            if "contrast" in kv:
                params["Contrast"] = params.get("Contrast", 0) + (float(kv["contrast"]) - 1) * 100
            if "saturation" in kv:
                params["Saturation"] = params.get("Saturation", 0) + (float(kv["saturation"]) - 1) * 100
            if "brightness" in kv:
                params["Brightness"] = params.get("Brightness", 0) + float(kv["brightness"]) * 100
            if "gamma" in kv:
                params["Brightness"] = params.get("Brightness", 0) + (float(kv["gamma"]) - 1) * 50
            extra = set(kv) - {"contrast", "saturation", "brightness", "gamma"}
            if extra:
                unmapped.append(f"eq:{','.join(sorted(extra))}")
        elif name == "curves":
            kv = _kv(body)
            master = kv.get("master") or kv.get("all")
            if master:
                pts = {}
                for p in master.split():
                    x, y = p.split("/")
                    pts[round(float(x), 2)] = float(y)
                if 0.25 in pts:
                    params["Shadows"] = params.get("Shadows", 0) + (pts[0.25] - 0.25) * 100
                if 0.75 in pts:
                    params["Highlights"] = params.get("Highlights", 0) + (pts[0.75] - 0.75) * 100
            else:
                unmapped.append("curves:" + ",".join(kv))
        elif name == "colorbalance":
            kv = {k: float(v) for k, v in _kv(body).items()}
            for zone, prefix in (("Shadows", "s"), ("Midtones", "m"), ("Highlights", "h")):
                r, g, bl = kv.get("r" + prefix, 0.0), kv.get("g" + prefix, 0.0), kv.get("b" + prefix, 0.0)
                if r or g or bl:
                    params[f"{zone} Warmth"] = params.get(f"{zone} Warmth", 0) + (r - bl) * 100
                    params[f"{zone} Tint"] = params.get(f"{zone} Tint", 0) + g * 100
        else:
            unmapped.append(name)
    return {k: round(_clamp(v), 3) for k, v in params.items() if abs(v) >= 0.05}, unmapped


def filter_video_xml(effect_ref: str, params: dict[str, float], sdr: bool = True) -> str:
    """<filter-video> block for Color Adjustments (DTD: after markers inside the clip)."""
    parts = [f'<filter-video ref="{effect_ref}" name={quoteattr(COLOR_ADJUSTMENTS_NAME)}>']
    parts.append(f'<param name="Control Range" key="19" value={quoteattr("0 (SDR)" if sdr else "1 (HDR)")}/>')
    for pname, key in PARAM_KEYS.items():
        if pname in params:
            parts.append(f'<param name={quoteattr(pname)} key="{key}" value="{params[pname]:g}"/>')
    parts.append("</filter-video>")
    return "".join(parts)


def effect_resource_xml(effect_id: str) -> str:
    return f'<effect id="{effect_id}" name={quoteattr(COLOR_ADJUSTMENTS_NAME)} uid={quoteattr(COLOR_ADJUSTMENTS_UID)}/>'
