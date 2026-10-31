#!/usr/bin/env python3
"""Export the open Final Cut project to a file, then loudness-normalise it with ffmpeg.

Flow (FCP has no dialog-free master export, so the Share sheet is driven):
  1. `share.export` with the "Export File (default)" destination → the Export File sheet
     (it may report "Menu execute failed" and still open the sheet; we go by `dialog.detect`)
  2. click "Next…" → NSSavePanel; type the destination path with Cmd+Shift+G (osascript) → Save
  3. wait until the file exists and stops growing, and FCP's background render is idle
  4. `render.apply_loudnorm_two_pass` (−14 LUFS / −1 dBTP / LRA 11, same as the ffmpeg path)
     → edit/final.mp4, unless --no-loudnorm
  5. ffprobe duration vs plan.json total

Usage:
    python helpers/fcp/fcp_export.py <edit_dir> [--name final_fcp.mov] [--no-loudnorm] [--timeout 1800]
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from bridge import Bridge, BridgeDown, BridgeError  # noqa: E402

HELPERS = Path(__file__).resolve().parent.parent
DESTINATION = "Export File (default)"


def _render():
    spec = importlib.util.spec_from_file_location("vu_render", HELPERS / "render.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _dialog(b: Bridge, title_contains: str, timeout: float = 15.0) -> dict | None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        d = b.call("dialog.detect")
        for dl in d.get("dialogs", []):
            if title_contains.lower() in str(dl.get("title", "")).lower() or \
               any(title_contains.lower() in str(l.get("text", "")).lower() for l in dl.get("labels", [])):
                return dl
        time.sleep(0.5)
    return None


def _type_path_in_panel(path: Path) -> None:
    script = f'''
    tell application "System Events"
        tell process "Final Cut Pro"
            set frontmost to true
            delay 0.5
            keystroke "g" using {{command down, shift down}}
            delay 0.8
            keystroke "{path.parent}"
            delay 0.4
            key code 36
            delay 0.8
            keystroke "a" using {{command down}}
            keystroke "{path.name}"
            delay 0.3
            key code 36
        end tell
    end tell'''
    subprocess.run(["osascript", "-e", script], check=True, capture_output=True, text=True)


def start_export(b: Bridge, out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if out_path.exists():
        out_path.unlink()
    try:
        b.call("share.export", destination=DESTINATION)
    except BridgeError as e:
        if "not found" in str(e):
            raise
        # "Menu execute failed" is reported even when the sheet opened; verify below
    sheet = _dialog(b, "Export File")
    if sheet is None:
        raise RuntimeError("the Export File sheet did not open")
    b.call("dialog.click", button="Next")
    time.sleep(1.5)
    _type_path_in_panel(out_path)
    time.sleep(1.5)
    # a "replace?" alert if the file existed
    d = b.call("dialog.detect")
    for dl in d.get("dialogs", []):
        titles = [bt.get("title", "") for bt in dl.get("buttons", [])]
        if any(t.lower().startswith("replace") for t in titles):
            b.call("dialog.click", button="Replace")


def wait_for_file(b: Bridge, out_path: Path, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    last_size, stable = -1, 0
    while time.monotonic() < deadline:
        time.sleep(3.0)
        size = out_path.stat().st_size if out_path.exists() else -1
        try:
            bg = b.call("backgroundRender.status")
            busy = bool(bg.get("rendering") or bg.get("active") or bg.get("exporting"))
        except BridgeError:
            busy = False
        if size > 0 and size == last_size and not busy:
            stable += 1
            if stable >= 2:
                return
        else:
            stable = 0
        last_size = size
    raise TimeoutError(f"export did not finish within {timeout:.0f}s ({out_path}, size {last_size})")


def loudnorm_reencode(src: Path, dst: Path, crf: str = "18", srt: Path | None = None) -> None:
    """Two-pass loudnorm like render.apply_loudnorm_two_pass, but re-encoding the video.

    FCP's Export File master is ProRes in .mov; `-c:v copy` into .mp4 is refused by
    ffmpeg, so the video goes through libx264 (CRF 18, same as upstream's final composite).
    """
    r = _render()
    m = r.measure_loudness(src)
    if m is None:
        raise RuntimeError(f"loudness measurement failed on {src}")
    af = (f"loudnorm=I={r.LOUDNORM_I}:TP={r.LOUDNORM_TP}:LRA={r.LOUDNORM_LRA}:"
          f"measured_I={m['input_i']}:measured_TP={m['input_tp']}:measured_LRA={m['input_lra']}:"
          f"measured_thresh={m['input_thresh']}:offset={m['target_offset']}:linear=true")
    vf = []
    if srt is not None:
        # FCP's Export File carries native captions as a separate track, never burned in;
        # burn master.srt here with upstream's exact style (Hard Rule 1: captions last).
        esc = str(srt.resolve()).replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")
        vf = ["-vf", f"subtitles='{esc}':force_style='{r.SUB_FORCE_STYLE}'"]
    cmd = ["ffmpeg", "-y", "-hide_banner", "-nostats", "-i", str(src), *vf,
           "-c:v", "libx264", "-preset", "medium", "-crf", crf, "-pix_fmt", "yuv420p",
           "-af", af, "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
           "-movflags", "+faststart", str(dst)]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg loudnorm failed: {proc.stderr[-800:]}")


def ffmpeg_has_subtitles() -> bool:
    out = subprocess.run(["ffmpeg", "-hide_banner", "-filters"], capture_output=True, text=True).stdout
    return any(line.split()[1:2] == ["subtitles"] for line in out.splitlines() if len(line.split()) > 1)


def probe_duration(path: Path) -> float:
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                          "-of", "csv=p=0", str(path)], capture_output=True, text=True)
    try:
        return float(out.stdout.strip())
    except ValueError:
        return 0.0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("edit_dir", type=Path)
    ap.add_argument("--name", default="final_fcp.mov")
    ap.add_argument("--final", default="final.mp4")
    ap.add_argument("--no-loudnorm", action="store_true")
    ap.add_argument("--burn-captions", choices=("auto", "yes", "no"), default="auto",
                    help="burn master.srt into final.mp4 (auto = when the plan used native captions)")
    ap.add_argument("--timeout", type=float, default=1800)
    ap.add_argument("--skip-export", action="store_true", help="only loudnorm + check an existing export")
    a = ap.parse_args()

    edit_dir = a.edit_dir.resolve()
    out = edit_dir / a.name
    plan = json.loads((edit_dir / "fcp" / "plan.json").read_text())
    b = Bridge()
    if not a.skip_export:
        try:
            b.version()
        except BridgeDown as e:
            print(f"bridge down: {e}")
            return 2
        r = b.call("project.open", name=plan["project"])
        if r.get("status") != "ok":
            print(f"could not open project {plan['project']!r}: {r}")
            return 1
        time.sleep(1.5)
        start_export(b, out)
        print(f"export started → {out}")
        wait_for_file(b, out, a.timeout)
    dur = probe_duration(out)
    frame = 1.0 / float(eval(plan["fps"]) if "/" in plan["fps"] else float(plan["fps"]))
    ok = abs(dur - plan["total_duration_s"]) <= frame + 1e-3
    print(f"{out.name}: {dur:.3f}s (plan {plan['total_duration_s']:.3f}s) {'ok' if ok else 'MISMATCH'}")
    if not a.no_loudnorm:
        final = edit_dir / a.final
        srt = edit_dir / "master.srt"
        burn = a.burn_captions == "yes" or (a.burn_captions == "auto" and plan.get("caption_mode") == "native")  # titles/both: FCP already burned them
        if burn and not srt.exists():
            print("no master.srt; captions not burned")
            burn = False
        if burn and not ffmpeg_has_subtitles():
            print("this ffmpeg has no libass (no `subtitles` filter): captions NOT burned.\n"
                  "  For burned-in captions let FCP render them: rebuild with "
                  "`edl_to_fcpxml.py --captions titles` (or `both`) and export again.")
            burn = False
        loudnorm_reencode(out, final, srt=srt if burn else None)
        print(f"loudnorm{' + burned captions' if burn else ''} → {final}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
