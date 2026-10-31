#!/usr/bin/env python3
"""Validate an FCPXML file before it goes anywhere near Final Cut.

1. DTD validation with xmllint against the vendored DTD (space-free path).
2. Semantic checks the DTD cannot express:
   - spine clips are contiguous (offset_n = offset_{n-1} + duration_{n-1})
   - sequence duration equals the sum of spine durations
   - every ref="…" resolves to a resource id
   - every media-rep src file exists on disk
   - anchored items on the same lane of the same parent do not overlap

Usage:
    python helpers/fcp/validate.py edit/fcp/project.fcpxml [--no-dtd] [--allow-missing-media]
Exit 0 when clean; non-zero with a readable list otherwise.
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
from fractions import Fraction
from pathlib import Path
from urllib.parse import unquote, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))
from timing import parse_rational  # noqa: E402

DTD_DIR = Path(__file__).resolve().parent / "dtd"
ANCHORABLE = {"asset-clip", "title", "caption", "clip", "video", "audio", "ref-clip", "sync-clip", "mc-clip"}


def dtd_for(version: str) -> Path:
    return DTD_DIR / f"FCPXMLv{version.replace('.', '_')}.dtd"


def dtd_validate(path: Path, version: str) -> list[str]:
    xmllint = shutil.which("xmllint")
    if not xmllint:
        return ["xmllint not found (brew install libxml2) — DTD validation skipped"]
    dtd = dtd_for(version)
    if not dtd.exists():
        return [f"no vendored DTD for version {version}: {dtd}"]
    proc = subprocess.run([xmllint, "--noout", "--dtdvalid", str(dtd), str(path)],
                          capture_output=True, text=True)
    if proc.returncode == 0:
        return []
    return [ln for ln in proc.stderr.splitlines() if ln.strip()][:40]


def semantic_checks(path: Path, allow_missing_media: bool = False) -> list[str]:
    errors: list[str] = []
    root = ET.parse(path).getroot()
    resources = root.find("resources")
    resource_ids = {el.get("id") for el in resources} if resources is not None else set()
    all_ids = {el.get("id") for el in root.iter() if el.get("id")}   # + text-style-def ids

    # refs resolve (text-style ref → text-style-def id; everything else → a resource id)
    for el in root.iter():
        ref = el.get("ref")
        if ref and el.tag == "text-style" and ref not in all_ids:
            errors.append(f"<text-style> references unknown text-style-def {ref!r}")
        elif ref and el.tag != "text-style" and ref not in resource_ids:
            errors.append(f"<{el.tag}> references unknown resource {ref!r}")
        fmt = el.get("format")
        if fmt and el.tag != "fcpxml" and fmt not in resource_ids:
            errors.append(f"<{el.tag}> format references unknown resource {fmt!r}")

    # media files exist
    if resources is not None and not allow_missing_media:
        for rep in resources.iter("media-rep"):
            src = rep.get("src", "")
            p = Path(unquote(urlparse(src).path)) if src.startswith("file:") else Path(src)
            if not p.exists():
                errors.append(f"media-rep src does not exist: {p}")

    for seq in root.iter("sequence"):
        spine = seq.find("spine")
        if spine is None:
            errors.append("sequence without spine")
            continue
        cursor = Fraction(0)
        for child in spine:
            if child.tag == "transition":
                continue
            off = parse_rational(child.get("offset", "0s"))
            dur = parse_rational(child.get("duration", "0s"))
            if off != cursor:
                errors.append(f"spine <{child.tag} name={child.get('name')!r}> offset {child.get('offset')} "
                              f"≠ expected {cursor} (gap or overlap)")
            cursor = off + dur
            _check_lane_overlaps(child, errors)
        seq_dur = seq.get("duration")
        if seq_dur and parse_rational(seq_dur) != cursor:
            errors.append(f"sequence duration {seq_dur} ≠ sum of spine {cursor}")
    return errors


def _check_lane_overlaps(parent: ET.Element, errors: list[str]) -> None:
    by_lane: dict[tuple[str, str], list[tuple[Fraction, Fraction, str]]] = {}
    for el in parent:
        if el.tag in ANCHORABLE and el.get("lane"):
            off = parse_rational(el.get("offset", "0s"))
            dur = parse_rational(el.get("duration", "0s"))
            by_lane.setdefault((el.tag, el.get("lane")), []).append((off, off + dur, el.get("name", "")))
    for (tag, lane), items in by_lane.items():
        items.sort()
        for (a0, a1, an), (b0, b1, bn) in zip(items, items[1:]):
            if b0 < a1:
                errors.append(f"<{tag}> lane {lane}: {an!r} ends {a1} after {bn!r} starts {b0} (overlap)")


def validate(path: Path, *, dtd: bool = True, allow_missing_media: bool = False) -> list[str]:
    errors: list[str] = []
    try:
        root = ET.parse(path).getroot()
    except ET.ParseError as e:
        return [f"not well-formed XML: {e}"]
    version = root.get("version", "1.11")
    if dtd:
        errors += dtd_validate(path, version)
    errors += semantic_checks(path, allow_missing_media=allow_missing_media)
    return errors


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("fcpxml", type=Path)
    ap.add_argument("--no-dtd", action="store_true")
    ap.add_argument("--allow-missing-media", action="store_true")
    a = ap.parse_args()
    errs = validate(a.fcpxml, dtd=not a.no_dtd, allow_missing_media=a.allow_missing_media)
    if errs:
        print(f"{a.fcpxml}: {len(errs)} problem(s)")
        for e in errs:
            print(f"  - {e}")
        return 1
    print(f"{a.fcpxml}: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
