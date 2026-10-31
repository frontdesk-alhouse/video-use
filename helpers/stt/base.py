"""Common contract for speech-to-text providers.

Every provider returns **Scribe-shaped** JSON so the rest of the pipeline
(`pack_transcripts.py`, `render.py`) never needs to know who transcribed:

    {
      "language_code": "en" | None,
      "text": "full transcript",
      "words": [{"type": "word"|"spacing"|"audio_event",
                 "text": str, "start": float, "end": float,
                 "speaker_id": "speaker_0" | None}],
      "provider": {"name", "engine", "version", "generated_at"},
    }

`pack_transcripts.py` reads only `words`, so the extra keys are safe.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

# Called as each file finishes: (video, payload) on success, (video, exception) on failure.
OnDone = Callable[[Path, "dict | Exception"], None]


class Provider(ABC):
    """A speech-to-text backend."""

    name: str = "base"
    # Whether batch mode may run several transcriptions of this provider at once.
    supports_parallel: bool = False

    @abstractmethod
    def transcribe(
        self,
        video: Path,
        *,
        audio_track: int = 0,
        language: str | None = None,
        num_speakers: int | None = None,
        force: bool = False,
        verbose: bool = False,
    ) -> dict:
        """Transcribe one video and return a Scribe-shaped payload."""

    def transcribe_many(
        self,
        videos: list[Path],
        *,
        audio_track: int = 0,
        language: str | None = None,
        num_speakers: int | None = None,
        force: bool = False,
        verbose: bool = False,
        workers: int = 1,
        on_done: OnDone | None = None,
    ) -> dict[Path, dict | Exception]:
        """Transcribe several videos. Default: one `transcribe` call per file, in order.

        A per-file failure is returned (and passed to `on_done`) as the exception
        instead of aborting the batch; a batch-wide failure may raise.
        """
        results: dict[Path, dict | Exception] = {}
        for v in videos:
            try:
                res: dict | Exception = self.transcribe(
                    v, audio_track=audio_track, language=language,
                    num_speakers=num_speakers, force=force, verbose=verbose)
            except Exception as e:  # reported per file, like upstream batch mode
                res = e
            results[v] = res
            if on_done:
                on_done(v, res)
        return results


def provider_meta(name: str, engine: str, version: str | None = None, **extra) -> dict:
    """The `provider` block stamped into every saved transcript."""
    return {
        "name": name,
        "engine": engine,
        "version": version,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        **extra,
    }
