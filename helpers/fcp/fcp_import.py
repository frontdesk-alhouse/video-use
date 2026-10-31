#!/usr/bin/env python3
"""Import edit/fcp/project.fcpxml into the running Final Cut Pro and reconcile it.

Steps:
  1. preflight: bridge alive, FCP version, no library save in progress
  2. import: small XML (JSON request < --inline-limit, default 60 000 bytes) → `fcpxml.import internal=True`;
     larger → write to disk and drive File > Import > XML (menu + osascript open panel)
  3. open the project by name (`project.open`), wait until the timeline shows it
  4. reconcile `timeline.getDetailedState` against plan.json → edit/fcp/import_check.json
     (clip count, each clip's start/end within --tolerance frames, total duration)
Exit 0 only when the reconciliation passes.

Usage:
    python helpers/fcp/fcp_import.py <edit_dir>/fcp/project.fcpxml [--plan plan.json] [--tolerance 1]
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from fractions import Fraction
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from bridge import Bridge, BridgeDown, BridgeError  # noqa: E402

# SpliceKit 3.3.9 parses each 64 KB socket read as its own message: a bigger inline request
# never parses and `fcpxml.import` hangs until the timeout. Measured on the JSON request.
INLINE_LIMIT = 60_000


def inline_request_bytes(xml: str) -> int:
    return len(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "fcpxml.import",
                           "params": {"xml": xml, "internal": True}}).encode("utf-8")) + 1


def import_route(xml: str, limit: int = INLINE_LIMIT, force_menu: bool = False) -> str:
    """'inline' when the whole JSON-RPC request fits one SpliceKit read, else 'menu'."""
    if force_menu or inline_request_bytes(xml) > limit:
        return "menu"
    return "inline"


def seconds_of(t: dict | None) -> float:
    if not t:
        return 0.0
    if "seconds" in t:
        return float(t["seconds"])
    return float(Fraction(int(t.get("value", 0)), int(t.get("timescale", 1))))


def preflight(b: Bridge) -> dict:
    v = b.version()
    try:
        upd = b.call("system.callMethod", className="FFLibraryDocument",
                     selector="isAnyLibraryUpdating", classMethod=True)
        if upd.get("result") in (1, True, "1"):
            raise RuntimeError("a library is being saved/updated — retry in a moment")
    except BridgeError:
        pass
    dlg = b.call("dialog.detect")
    blocking = [d for d in dlg.get("dialogs", []) if d.get("isModal") or d.get("isSheet")]
    if blocking:   # non-modal panels (e.g. FFOSCOverlayWindow, on-screen controls) are fine
        raise RuntimeError(f"FCP has a modal dialog open; close it first: {[d.get('title') for d in blocking]}")
    return v


def import_inline(b: Bridge, xml: str) -> dict:
    r = b.call("fcpxml.import", _timeout=300, xml=xml, internal=True)
    if not r.get("importOK", r.get("status") == "ok"):
        raise RuntimeError(f"fcpxml.import failed: {r}")
    return r


def import_via_menu(b: Bridge, path: Path) -> dict:
    """File > Import > XML…, then type the path into the NSOpenPanel (Cmd+Shift+G)."""
    try:
        r = b.call("menu.execute", menuPath=["File", "Import", "XML"])
    except BridgeError as e:
        r = {"warning": str(e)}   # SpliceKit reports "Menu execute failed" even when the panel opens
    for _ in range(10):
        time.sleep(0.5)
        d = b.call("dialog.detect")
        if any("import" in str(dl.get("title", "")).lower() for dl in d.get("dialogs", [])):
            break
    else:
        raise RuntimeError(f"File > Import > XML panel did not open ({r})")
    # the panel may open on another screen and miss keystrokes: activate + raise it first
    script = f'''
    tell application "Final Cut Pro" to activate
    delay 0.4
    tell application "System Events"
        tell process "Final Cut Pro"
            set frontmost to true
            try
                perform action "AXRaise" of window "Import XML"
            end try
            delay 0.5
            keystroke "g" using {{command down, shift down}}
            delay 0.8
            keystroke "{str(path)}"
            delay 0.4
            key code 36
            delay 0.8
            key code 36
        end tell
    end tell'''
    subprocess.run(["osascript", "-e", script], check=True, capture_output=True, text=True)
    # a wide-gamut library asks for the color mode → Continue
    for _ in range(10):
        time.sleep(1.0)
        dlg = b.call("dialog.detect")
        if not any(d.get("isModal") or d.get("isSheet") for d in dlg.get("dialogs", [])):
            break
        buttons = [bt.get("title", "") for d in dlg.get("dialogs", []) for bt in d.get("buttons", [])]
        for want in ("Continue", "OK", "Import"):   # color-mode sheet; "imported with warnings" alert
            if any(want.lower() == t.lower() for t in buttons):
                b.call("dialog.click", button=want)
                break
    return {"status": "ok", "route": "menu", "menu": r}


def wait_for_project(b: Bridge, name: str, timeout: float = 60.0) -> dict:
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        try:
            r = b.call("project.open", name=name)
            if r.get("status") == "ok":
                time.sleep(1.5)
                st = b.call("timeline.getDetailedState", limit=500)
                if st.get("sequenceName") == name or name in str(st.get("sequenceName", "")):
                    st["_library"] = r.get("library")
                    return st
                last = st.get("sequenceName")
        except BridgeError as e:
            last = str(e)
        time.sleep(2.0)
    raise TimeoutError(f"project {name!r} did not open (last seen: {last})")


def reconcile(state: dict, plan: dict, tolerance_frames: int = 1) -> dict:
    frame = 1.0 / float(Fraction(plan["fps"]))
    tol = tolerance_frames * frame + 1e-6
    spine = [it for it in state.get("items", []) if it.get("lane", 0) == 0
             and "Transition" not in it.get("class", "")]
    expected = plan["clips"]
    problems: list[str] = []
    rows = []
    # gaps may not be reported as timeline items; if the counts only differ by them, match without
    non_gap = [c for c in expected if c.get("kind") != "gap"]
    if len(spine) != len(expected) and len(spine) == len(non_gap) < len(expected):
        expected = non_gap
    if len(spine) != len(expected):
        problems.append(f"spine has {len(spine)} items, plan has {len(expected)} clips")
    for i, exp in enumerate(expected):
        if i >= len(spine):
            break
        got = spine[i]
        # endTime comes back on a coarse timescale (e.g. 3s @ timescale 1 for 3.2s);
        # start + duration is exact, so derive the end from those.
        gs = seconds_of(got.get("startTime"))
        ge = gs + seconds_of(got.get("duration")) if got.get("duration") else seconds_of(got.get("endTime"))
        if "startTime" not in got:
            gd = seconds_of(got.get("duration"))
            gs = ge = None
            ok = abs(gd - exp["duration_s"]) <= tol
            rows.append({"index": i, "name": got.get("name"), "duration_s": gd,
                         "expected_duration_s": exp["duration_s"], "ok": ok})
            if not ok:
                problems.append(f"clip {i} duration {gd:.3f} ≠ {exp['duration_s']:.3f}")
            continue
        ok = abs(gs - exp["out_start_s"]) <= tol and abs(ge - exp["out_end_s"]) <= tol
        rows.append({"index": i, "name": got.get("name"), "start_s": gs, "end_s": ge,
                     "expected_start_s": exp["out_start_s"], "expected_end_s": exp["out_end_s"], "ok": ok})
        if not ok:
            problems.append(f"clip {i} {got.get('name')!r}: {gs:.3f}–{ge:.3f} ≠ "
                            f"{exp['out_start_s']:.3f}–{exp['out_end_s']:.3f}")
    total = seconds_of(state.get("duration"))
    if spine and "startTime" in spine[-1] and spine[-1].get("duration"):
        total = seconds_of(spine[-1]["startTime"]) + seconds_of(spine[-1]["duration"])
    elif spine and not total:
        total = sum(seconds_of(it.get("duration")) for it in spine)
    if abs(total - plan["total_duration_s"]) > tol:
        problems.append(f"timeline duration {total:.3f} ≠ plan {plan['total_duration_s']:.3f}")
    return {"ok": not problems, "problems": problems, "clips": rows,
            "timeline_duration_s": total, "plan_duration_s": plan["total_duration_s"],
            "sequence": state.get("sequenceName"), "item_count": state.get("itemCount")}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("fcpxml", type=Path)
    ap.add_argument("--plan", type=Path, default=None, help="default: plan.json next to the fcpxml")
    ap.add_argument("--tolerance", type=int, default=1, help="frames of slack per edge (default 1)")
    ap.add_argument("--inline-limit", type=int, default=INLINE_LIMIT)
    ap.add_argument("--force-menu", action="store_true", help="always use File > Import > XML")
    ap.add_argument("--skip-import", action="store_true", help="only open + reconcile")
    a = ap.parse_args()

    plan_path = a.plan or a.fcpxml.with_name("plan.json")
    plan = json.loads(plan_path.read_text())
    b = Bridge()
    try:
        v = preflight(b)
    except BridgeDown as e:
        print(f"bridge down → fall back to `python helpers/render.py` (ffmpeg path)\n  {e}")
        return 2
    print(f"FCP {v.get('fcp_version')} · SpliceKit {v.get('splicekit_version')}")

    if not a.skip_import:
        xml_text = a.fcpxml.read_text(encoding="utf-8")
        xml_bytes = inline_request_bytes(xml_text)
        if import_route(xml_text, a.inline_limit, a.force_menu) == "menu":
            print(f"import via File > Import > XML ({xml_bytes/1024:.1f} KB request > {a.inline_limit} bytes)")
            import_via_menu(b, a.fcpxml.resolve())
        else:
            print(f"import inline ({xml_bytes/1024:.1f} KB)")
            import_inline(b, xml_text)

    state = wait_for_project(b, plan["project"])
    check = reconcile(state, plan, a.tolerance)
    check["library"] = state.get("_library")
    print(f"landed in library {check['library']!r} (the menu route uses the library selected in the browser)")
    out = a.fcpxml.with_name("import_check.json")
    out.write_text(json.dumps(check, indent=2, ensure_ascii=False))
    print(f"project {check['sequence']!r}: {check['item_count']} items, "
          f"{check['timeline_duration_s']:.3f}s (plan {check['plan_duration_s']:.3f}s)")
    for row in check["clips"]:
        mark = "ok " if row["ok"] else "BAD"
        print(f"  {mark} {row['index']:2d} {row.get('name','')!r}")
    if not check["ok"]:
        print("reconciliation FAILED:")
        for p in check["problems"]:
            print(f"  - {p}")
        return 1
    print(f"reconciliation OK → {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
