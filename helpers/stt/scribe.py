"""ElevenLabs Scribe provider: a thin adapter over the upstream helpers in transcribe.py."""
from __future__ import annotations

import os
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from .base import OnDone, Provider, provider_meta


def _transcribe_module():
    # Imported lazily: transcribe.py imports this package, so a top-level import would cycle.
    import transcribe
    return transcribe


def find_api_key() -> str | None:
    """Like `transcribe.load_api_key`, but returns None instead of exiting."""
    for candidate in [Path(__file__).resolve().parents[2] / ".env", Path(".env")]:
        if candidate.exists():
            for line in candidate.read_text().splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                if k.strip() == "ELEVENLABS_API_KEY":
                    return v.strip().strip('"').strip("'") or None
    return os.environ.get("ELEVENLABS_API_KEY") or None


class ScribeProvider(Provider):
    name = "scribe"
    supports_parallel = True

    def __init__(self, api_key: str | None = None):
        self._api_key = api_key

    @property
    def api_key(self) -> str:
        if self._api_key is None:
            self._api_key = _transcribe_module().load_api_key()  # exits when missing, as upstream
        return self._api_key

    def transcribe(self, video: Path, *, audio_track: int = 0, language: str | None = None,
                   num_speakers: int | None = None, force: bool = False,
                   verbose: bool = False) -> dict:
        t = _transcribe_module()
        n_tracks = t.count_audio_tracks(video)
        if n_tracks > 1 and verbose:
            print(f"  note: {video.name} has {n_tracks} audio tracks, using track "
                  f"{audio_track + 1} (--audio-track to change)", flush=True)

        with tempfile.TemporaryDirectory() as tmp:
            audio = Path(tmp) / f"{video.stem}.wav"
            t.extract_audio(video, audio, audio_track)

            # Uploading silence costs the same as uploading speech and returns
            # nothing, so catch the wrong-track case before paying for it.
            peak = t.peak_dbfs(audio)
            if peak < -60.0:
                raise RuntimeError(
                    f"track {audio_track + 1} of {video.name} is silent "
                    f"(peak {peak:.1f} dBFS) - not uploading. "
                    + (f"The file has {n_tracks} audio tracks; try --audio-track "
                       + " or ".join(str(i) for i in range(n_tracks) if i != audio_track) + "."
                       if n_tracks > 1 else "Check the source audio.")
                )

            size_mb = audio.stat().st_size / (1024 * 1024)
            if verbose:
                print(f"  uploading {video.stem}.wav ({size_mb:.1f} MB)", flush=True)
            payload = t.call_scribe(audio, self.api_key, language, num_speakers)

        if isinstance(payload, dict):
            payload.setdefault("provider", provider_meta("scribe", "scribe_v1"))
        return payload



    def transcribe_many(self, videos: list[Path], *, audio_track: int = 0,
                        language: str | None = None, num_speakers: int | None = None,
                        force: bool = False, verbose: bool = False, workers: int = 4,
                        on_done: OnDone | None = None) -> dict[Path, dict | Exception]:
        """Independent uploads, so run them on `workers` threads (upstream batch behaviour)."""
        _ = self.api_key  # resolve once, before the threads (exits when missing, as upstream)
        results: dict[Path, dict | Exception] = {}
        with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
            futures = {
                pool.submit(self.transcribe, v, audio_track=audio_track, language=language,
                            num_speakers=num_speakers, force=force, verbose=verbose): v
                for v in videos
            }
            for fut in as_completed(futures):
                v = futures[fut]
                try:
                    res: dict | Exception = fut.result()
                except Exception as e:
                    res = e
                results[v] = res
                if on_done:
                    on_done(v, res)
        return results
