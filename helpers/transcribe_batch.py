"""Batch-transcribe every video in a directory with 4 parallel workers.

Walks <videos_dir> for common video extensions, runs ElevenLabs Scribe on
each, writes transcripts to <videos_dir>/edit/transcripts/<name>.json.

Cached per-file: any source that already has a transcript is skipped.

Usage:
    python helpers/transcribe_batch.py <videos_dir>
    python helpers/transcribe_batch.py <videos_dir> --workers 4
    python helpers/transcribe_batch.py <videos_dir> --num-speakers 2
    python helpers/transcribe_batch.py <videos_dir> --edit-dir /custom/edit
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from stt import resolve_provider
from transcribe import save_transcript, transcript_path


VIDEO_EXTS = {".mp4", ".MP4", ".mov", ".MOV", ".mkv", ".MKV", ".avi", ".AVI", ".m4v"}


def find_videos(videos_dir: Path) -> list[Path]:
    videos = sorted(
        p for p in videos_dir.iterdir()
        if p.is_file() and p.suffix in VIDEO_EXTS
    )
    return videos


def main() -> None:
    ap = argparse.ArgumentParser(description="Parallel batch transcription of a videos directory")
    ap.add_argument("videos_dir", type=Path, help="Directory containing source videos")
    ap.add_argument(
        "--edit-dir",
        type=Path,
        default=None,
        help="Edit output directory (default: <videos_dir>/edit)",
    )
    ap.add_argument("--workers", type=int, default=4, help="Parallel workers (default: 4)")
    ap.add_argument(
        "--language",
        type=str,
        default=None,
        help="Optional ISO language code. Omit to auto-detect per file.",
    )
    ap.add_argument(
        "--num-speakers",
        type=int,
        default=None,
        help="Optional number of speakers. Improves diarization when known.",
    )
    ap.add_argument(
        "--audio-track",
        type=int,
        default=0,
        help="Zero-based audio track to transcribe (OBS: 0 = game, 1 = mic).",
    )
    ap.add_argument(
        "--provider",
        choices=["auto", "fcp", "scribe"],
        default=None,
        help="Speech-to-text provider (default: $VIDEO_USE_STT or auto).",
    )
    args = ap.parse_args()

    videos_dir = args.videos_dir.resolve()
    if not videos_dir.is_dir():
        sys.exit(f"not a directory: {videos_dir}")

    edit_dir = (args.edit_dir or (videos_dir / "edit")).resolve()
    (edit_dir / "transcripts").mkdir(parents=True, exist_ok=True)

    videos = find_videos(videos_dir)
    if not videos:
        sys.exit(f"no videos found in {videos_dir}")

    already_cached = [v for v in videos
                      if transcript_path(edit_dir, v, args.audio_track).exists()]
    pending = [v for v in videos if v not in already_cached]

    print(f"found {len(videos)} videos ({len(already_cached)} cached, {len(pending)} to transcribe)")
    if not pending:
        print("nothing to do")
        return

    provider = resolve_provider(args.provider)
    workers = args.workers if provider.supports_parallel else 1

    print(f"transcribing {len(pending)} files with {workers} parallel workers")
    t0 = time.time()

    errors: list[tuple[Path, str]] = []

    def on_done(v: Path, res: dict | Exception) -> None:
        if isinstance(res, Exception):
            errors.append((v, str(res)))
            print(f"  x {v.stem}  FAILED: {res}")
            return
        out = save_transcript(transcript_path(edit_dir, v, args.audio_track), res, provider.name)
        print(f"  + {v.stem}  →  {out.name}")

    # One call for the whole batch: Scribe fans out to `workers` threads, FCP puts
    # every pending take on one inventory timeline and transcribes it once.
    try:
        provider.transcribe_many(
            pending,
            audio_track=args.audio_track,
            language=args.language,
            num_speakers=args.num_speakers,
            workers=workers,
            on_done=on_done,
        )
    except Exception as e:
        done = {v for v, _ in errors} | {v for v in pending
                                         if transcript_path(edit_dir, v, args.audio_track).exists()}
        for v in pending:
            if v not in done:
                errors.append((v, str(e)))
        print(f"  x batch FAILED: {e}")

    dt = time.time() - t0
    print(f"\ndone in {dt:.1f}s")
    if errors:
        print(f"{len(errors)} failures:")
        for v, msg in errors:
            print(f"  {v.name}: {msg}")
        sys.exit(1)


if __name__ == "__main__":
    main()
