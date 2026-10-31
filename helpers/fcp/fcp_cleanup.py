#!/usr/bin/env python3
"""List — and only on request, trash — the scratch projects video-use leaves in Final Cut.

video-use creates projects named "video-use · …" (inventory timelines for transcription,
probes) and, per edit, "<videos_dir> · <date>". This tool only ever touches projects whose
name starts with one of the --prefix values (default: "video-use · "), so a real edit is
never matched unless you ask for its prefix explicitly.

    python helpers/fcp/fcp_cleanup.py                        # dry run: list matches
    python helpers/fcp/fcp_cleanup.py --delete               # trash every match
    python helpers/fcp/fcp_cleanup.py --prefix "fixture · " --delete

Deleting = project.open → revealProjectInBrowser → moveToTrash (FCP's Move to Trash; the
project goes to the library's trash, recoverable with Edit > Undo while FCP stays open).
"""
from __future__ import annotations

import argparse
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from bridge import Bridge, BridgeDown, BridgeError  # noqa: E402

DEFAULT_PREFIXES = ("video-use · ",)


def list_projects(b: Bridge) -> list[tuple[str, str]]:
    """(name, library) of every project FCP knows, read from project.open's 'available' list."""
    try:
        b.call("project.open", name="\u0000no-such-project\u0000")
    except BridgeError as e:
        err = e.error
        text = err.get("message", "") if isinstance(err, dict) else str(err)
    else:
        return []
    names = re.findall(r'library = "([^"]*)";\s*name = (?:"((?:[^"\\]|\\.)*)"|([^;]+));', text)
    out = []
    for lib, quoted, bare in names:
        name = (quoted or bare).strip()
        name = re.sub(r"\\U([0-9a-fA-F]{4})", lambda m: chr(int(m.group(1), 16)), name)
        out.append((name, lib))
    return sorted(set(out))


def trash_project(b: Bridge, name: str) -> None:
    r = b.call("project.open", name=name)
    if r.get("project") != name:
        raise RuntimeError(f"project.open matched {r.get('project')!r}, not {name!r}; refusing to trash")
    time.sleep(1.0)
    b.call("timeline.action", action="revealProjectInBrowser")
    time.sleep(0.8)
    b.call("timeline.action", action="moveToTrash")
    time.sleep(1.0)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--prefix", action="append", help=f"name prefix to match (default: {DEFAULT_PREFIXES})")
    ap.add_argument("--delete", action="store_true", help="move the matches to the trash (default: list only)")
    a = ap.parse_args()
    prefixes = tuple(a.prefix or DEFAULT_PREFIXES)
    b = Bridge(timeout=120)
    try:
        projects = list_projects(b)
    except BridgeDown as e:
        print(f"bridge down: {e}")
        return 2
    matches = [(n, lib) for n, lib in projects if n.startswith(prefixes)]
    print(f"{len(matches)} project(s) matching {prefixes}:")
    for n, lib in matches:
        print(f"  {n}   [{lib}]")
    if not a.delete or not matches:
        if matches:
            print("dry run — add --delete to move them to the trash")
        return 0
    failed = 0
    for n, lib in matches:
        before = set(list_projects(b))
        try:
            trash_project(b, n)
        except (BridgeError, RuntimeError) as e:
            failed += 1
            print(f"  FAILED {n}: {e}")
            continue
        gone = before - set(list_projects(b))
        if gone != {(n, lib)}:
            # Move to Trash acts on the browser selection; if anything else vanished, undo and stop.
            b.call("timeline.action", action="undo")
            print(f"  ABORTED at {n!r}: expected only it to disappear, gone={sorted(gone)}; undone")
            return 1
        print(f"  trashed {n}")
    left = {n for n, _ in list_projects(b)} & {n for n, _ in matches}
    if left:
        print(f"still present: {sorted(left)}")
    return 1 if failed or left else 0


if __name__ == "__main__":
    sys.exit(main())
