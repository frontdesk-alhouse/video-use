#!/usr/bin/env python3
"""Verify the imported Final Cut project against edit/fcp/plan.json.

What it checks (bridge up, project open):
  - round-trip XML (`fcpxml.export`) → clip count, one fadeIn + fadeOut per audio clip,
    markers with the beat names, caption count vs plan, grade params present
  - captions parity → caption cue texts/times from the round-trip XML vs master.srt
    (same chunking, so texts must match and times stay within --tolerance frames)
  - visual → viewer captures 2 frames before and after every cut (+ one timeline
    capture); identical consecutive PNGs are flagged (the viewer capture is known
    to repeat a stale image), so screenshots are evidence, not proof
  - pacing → `timeline.getDetailedState` flags clips shorter than 0.5 s
Writes edit/verify/fcp_report.json and exits non-zero on any hard failure.

Usage:
    python helpers/fcp/fcp_verify.py <edit_dir> [--tolerance 1] [--no-captures]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import time
import xml.etree.ElementTree as ET
from fractions import Fraction
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from bridge import Bridge, BridgeDown, BridgeError  # noqa: E402
from timing import parse_rational  # noqa: E402

SRT_TIME = re.compile(r"(\d+):(\d+):(\d+)[,.](\d+)\s*-->\s*(\d+):(\d+):(\d+)[,.](\d+)")


def parse_srt(path: Path) -> list[tuple[float, float, str]]:
    cues = []
    blocks = re.split(r"\n\s*\n", path.read_text(encoding="utf-8").strip())
    for blk in blocks:
        lines = [ln for ln in blk.splitlines() if ln.strip()]
        if len(lines) < 2:
            continue
        m = SRT_TIME.search(lines[1] if SRT_TIME.search(lines[1]) else lines[0])
        if not m:
            continue
        h1, m1, s1, ms1, h2, m2, s2, ms2 = (int(x) for x in m.groups())
        a = h1 * 3600 + m1 * 60 + s1 + ms1 / 1000
        bb = h2 * 3600 + m2 * 60 + s2 + ms2 / 1000
        text = " ".join(lines[2:] if SRT_TIME.search(lines[1]) else lines[1:]).strip()
        cues.append((a, bb, text))
    return cues


def analyse_roundtrip(xml_path: Path, plan: dict) -> dict:
    root = ET.parse(xml_path).getroot()
    spine = root.find(".//spine")
    clips = [c for c in spine if c.tag != "transition"] if spine is not None else []
    out = {"clip_count": len(clips), "expected_clips": len(plan["clips"]), "problems": []}
    if len(clips) != len(plan["clips"]):
        out["problems"].append(f"round-trip spine has {len(clips)} clips, plan {len(plan['clips'])}")

    fades_ok, markers, captions, grades = 0, [], [], 0
    expected_fades = sum(1 for c in plan["clips"] if c.get("has_audio")) if plan.get("fade_frames") else 0
    for ci, clip in enumerate(clips):
        if clip.find("./adjust-volume/param/fadeIn") is not None and clip.find("./adjust-volume/param/fadeOut") is not None:
            fades_ok += 1
        for mk in clip.findall("./marker"):
            markers.append(mk.get("value"))
        if clip.tag != "asset-clip":        # gap / card still: titles there are card text, not captions
            continue
        clip_start = parse_rational(clip.get("start", "0s"))
        clip_offset = parse_rational(clip.get("offset", "0s"))
        mode = plan.get("caption_mode", "native")
        elems = clip.findall("./title") if mode == "titles" else clip.findall("./caption")
        if mode == "both":
            out.setdefault("titles", 0)
            out["titles"] += len(clip.findall("./title"))
        for cap in elems:
            off = parse_rational(cap.get("offset", "0s"))
            dur = parse_rational(cap.get("duration", "0s"))
            text = "".join(cap.find("text").itertext()).strip() if cap.find("text") is not None else cap.get("name", "")
            out_start = float(clip_offset + (off - clip_start))
            captions.append({"clip_index": ci, "text": text, "out_start_s": out_start, "out_end_s": out_start + float(dur)})
        if clip.find("./filter-video") is not None:
            grades += 1
    out["fades"] = {"ok": fades_ok, "expected": expected_fades}
    if fades_ok != expected_fades:
        out["problems"].append(f"{fades_ok} clips carry fadeIn+fadeOut, expected {expected_fades}")
    exp_markers = [m["value"] for m in plan["markers"]]
    out["markers"] = {"got": markers, "expected": exp_markers}
    if markers != exp_markers:
        out["problems"].append(f"markers {markers} ≠ {exp_markers}")
    out["captions"] = {"got": len(captions), "expected": len(plan["captions"])}
    if len(captions) != len(plan["captions"]):
        out["problems"].append(f"{len(captions)} captions in FCP, plan has {len(plan['captions'])}")
    if plan.get("caption_mode") == "both" and out.get("titles", 0) != len(plan["captions"]):
        out["problems"].append(f"{out.get('titles', 0)} burned titles, plan has {len(plan['captions'])}")
    out["grade_clips"] = grades
    if plan.get("grade", {}).get("params") or plan.get("grade", {}).get("auto"):
        media = sum(1 for c in clips if c.tag == "asset-clip")
        if grades != media:
            out["problems"].append(f"grade on {grades}/{media} clips")
    out["_captions"] = captions
    return out


def captions_parity(captions: list[dict], srt_path: Path, tol_s: float) -> dict:
    res = {"srt_cues": 0, "fcp_cues": len(captions), "mismatches": []}
    if not srt_path.exists():
        res["mismatches"].append("master.srt missing")
        return res
    srt = parse_srt(srt_path)
    res["srt_cues"] = len(srt)
    fcp = sorted(captions, key=lambda c: c["out_start_s"])
    for i, (a, b_, text) in enumerate(srt):
        if i >= len(fcp):
            res["mismatches"].append(f"srt cue {i} {text!r} has no FCP caption")
            continue
        c = fcp[i]
        if c["text"].upper() != text.upper():
            res["mismatches"].append(f"cue {i}: FCP {c['text']!r} ≠ SRT {text!r}")
        elif abs(c["out_start_s"] - a) > tol_s:
            res["mismatches"].append(f"cue {i} {text!r}: FCP start {c['out_start_s']:.3f} vs SRT {a:.3f}")
    res["mismatches"] = res["mismatches"][:25]
    return res


def captures(b: Bridge, plan: dict, verify_dir: Path, frame_s: float) -> dict:
    verify_dir.mkdir(parents=True, exist_ok=True)
    hashes: list[str] = []
    files: list[str] = []
    dup = 0
    cuts = [c["out_start_s"] for c in plan["clips"][1:]]
    for n, t in enumerate(cuts, start=1):
        for tag, tt in (("a", max(0.0, t - 2 * frame_s)), ("b", t + 2 * frame_s)):
            b.call("playback.seekToTime", seconds=tt)
            time.sleep(0.4)
            p = verify_dir / f"cut_{n:02d}_{tag}.png"
            b.call("viewer.capture", path=str(p))
            if p.exists():
                h = hashlib.md5(p.read_bytes()).hexdigest()
                if hashes and h == hashes[-1]:
                    dup += 1
                hashes.append(h)
                files.append(str(p))
    tl = verify_dir / "timeline.png"
    b.call("timeline.capture", path=str(tl))
    return {"files": files, "timeline": str(tl), "duplicate_consecutive": dup}


def frames_from_export(video: Path, plan: dict, verify_dir: Path, frame_s: float) -> dict:
    """Grab the frames 2 before / 2 after every cut from FCP's own render (ground truth),
    plus a contact sheet. Unlike `viewer.capture` this cannot pick the wrong pane."""
    import subprocess
    verify_dir.mkdir(parents=True, exist_ok=True)
    files: list[str] = []
    cuts = [c["out_start_s"] for c in plan["clips"][1:]] or [plan["clips"][0]["out_start_s"] + 0.5]
    for n, t in enumerate(cuts, start=1):
        for tag, tt in (("a", max(0.0, t - 2 * frame_s)), ("b", t + 2 * frame_s)):
            out = verify_dir / f"cut_{n:02d}_{tag}.png"
            subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", f"{tt:.4f}", "-i", str(video),
                            "-frames:v", "1", "-vf", "scale=480:-2", str(out)], check=True)
            files.append(str(out))
    sheet = verify_dir / "cuts_sheet.png"
    if len(files) > 1:
        inputs = sum((["-i", f] for f in files), [])
        pads = "".join(f"[{i}:v]" for i in range(len(files)))
        layout = "|".join(_cell(i, 2) for i in range(len(files)))
        subprocess.run(["ffmpeg", "-v", "error", "-y", *inputs, "-filter_complex",
                        f"{pads}xstack=inputs={len(files)}:layout={layout}", str(sheet)], check=False)
    return {"source": str(video), "files": files, "sheet": str(sheet) if sheet.exists() else None}


def _cell(i: int, cols: int) -> str:
    """xstack layout cell for index i in a grid of `cols` columns (all cells same size)."""
    c, r = i % cols, i // cols
    x = "+".join(["w0"] * c) or "0"
    y = "+".join(["h0"] * r) or "0"
    return f"{x}_{y}"


def pacing(b: Bridge) -> dict:
    st = b.call("timeline.getDetailedState", limit=500)
    items = [i for i in st.get("items", []) if "Transition" not in i.get("class", "")]
    short = [i.get("name") for i in items if float(i.get("duration", {}).get("seconds", 0)) < 0.5]
    return {"items": len(items), "short_clips": short}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("edit_dir", type=Path)
    ap.add_argument("--tolerance", type=int, default=1, help="frames")
    ap.add_argument("--no-captures", action="store_true")
    ap.add_argument("--frames", choices=("auto", "export", "viewer"), default="auto",
                    help="cut frames from the exported render (default when edit/final_fcp.mov exists) "
                         "or from viewer.capture")
    a = ap.parse_args()
    edit_dir = a.edit_dir.resolve()
    plan = json.loads((edit_dir / "fcp" / "plan.json").read_text())
    frame_s = 1.0 / float(Fraction(plan["fps"]))
    b = Bridge()
    try:
        b.version()
    except BridgeDown as e:
        print(f"bridge down: {e}")
        return 2
    rt = edit_dir / "fcp" / "roundtrip.fcpxml"
    r = b.call("fcpxml.export", path=str(rt))
    if r.get("status") != "ok":
        print(f"fcpxml.export failed: {r}")
        return 1
    report = {"roundtrip": analyse_roundtrip(rt, plan)}
    caps = report["roundtrip"].pop("_captions")
    if plan.get("caption_mode", "native") != "none":
        report["captions_parity"] = captions_parity(caps, edit_dir / "master.srt", a.tolerance * frame_s + 1e-3)
    report["pacing"] = pacing(b)
    export = edit_dir / "final_fcp.mov"
    use_export = a.frames == "export" or (a.frames == "auto" and export.exists())
    if not a.no_captures and use_export:
        report["export_frames"] = frames_from_export(export, plan, edit_dir / "verify", frame_s)
    elif not a.no_captures:
        report["captures"] = captures(b, plan, edit_dir / "verify", frame_s)
    hard = list(report["roundtrip"]["problems"])
    hard += report.get("captions_parity", {}).get("mismatches", [])
    report["ok"] = not hard
    out = edit_dir / "verify" / "fcp_report.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2, ensure_ascii=False))
    rr = report["roundtrip"]
    print(f"clips {rr['clip_count']}/{rr['expected_clips']} · fades {rr['fades']['ok']}/{rr['fades']['expected']} · "
          f"markers {len(rr['markers']['got'])} · captions {rr['captions']['got']}/{rr['captions']['expected']} · "
          f"graded {rr['grade_clips']}")
    if "captions_parity" in report:
        cp = report["captions_parity"]
        print(f"captions parity: {cp['fcp_cues']} FCP vs {cp['srt_cues']} SRT, {len(cp['mismatches'])} mismatches")
    if "captures" in report:
        c = report["captures"]
        print(f"captures: {len(c['files'])} viewer PNGs, {c['duplicate_consecutive']} duplicate consecutive (suspect)")
    if "export_frames" in report:
        print(f"cut frames from the render: {len(report['export_frames']['files'])} PNGs, "
              f"sheet {report['export_frames']['sheet']}")
    if report["pacing"]["short_clips"]:
        print(f"short clips (<0.5s): {report['pacing']['short_clips']}")
    for h in hard:
        print(f"  - {h}")
    print(f"report → {out}  {'OK' if report['ok'] else 'FAILED'}")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
