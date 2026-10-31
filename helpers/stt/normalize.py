"""Pure conversions from provider-native word lists to Scribe-shaped words."""
from __future__ import annotations

import re

_TRAILING_INT = re.compile(r"(\d+)\s*$")


def normalize_speaker(speaker: object, prefix: str = "speaker_") -> str | None:
    """Map a provider speaker label to Scribe's `speaker_<n>` form.

    0 -> "speaker_0", "Speaker 1" -> "speaker_1", "speaker_2" -> "speaker_2",
    None / "" / "Unknown" -> None. Labels without a number are slugged ("Host" -> "speaker_host").
    """
    if speaker is None or isinstance(speaker, bool):
        return None
    if isinstance(speaker, (int, float)):
        return f"{prefix}{int(speaker)}"
    label = str(speaker).strip()
    if not label or label.lower() in {"unknown", "none", "null"}:
        return None
    if label.startswith(prefix):
        return label
    m = _TRAILING_INT.search(label)
    if m:
        return f"{prefix}{int(m.group(1))}"
    slug = re.sub(r"[^a-z0-9]+", "_", label.lower()).strip("_")
    return f"{prefix}{slug}" if slug else None


def fcp_words_to_scribe(words: list[dict], *, speaker_prefix: str = "speaker_",
                        offset: float = 0.0) -> list[dict]:
    """Convert SpliceKit `transcript.getState` words to Scribe `words` entries.

    Input entries: {index, text, startTime, endTime, duration, confidence, speaker}.
    Output: `word` entries, with a `spacing` entry between two words only when
    there is a real gap (next.start > prev.end) — `pack_transcripts` reads those
    gaps to break phrases. No `audio_event` entries: FCP does not tag them.
    `offset` is subtracted from every time (timeline seconds -> source seconds).
    """
    out: list[dict] = []
    prev_end: float | None = None
    for w in sorted(words, key=lambda x: float(x.get("startTime", 0.0))):
        text = w.get("text")
        if text is None or w.get("startTime") is None:
            continue
        start = round(float(w["startTime"]) - offset, 6)
        end = round(float(w.get("endTime", w["startTime"])) - offset, 6)
        if prev_end is not None and start > prev_end:
            out.append({"type": "spacing", "text": " ", "start": prev_end, "end": start})
        out.append({
            "type": "word",
            "text": str(text),
            "start": start,
            "end": end,
            "speaker_id": normalize_speaker(w.get("speaker"), speaker_prefix),
        })
        prev_end = end
    return out


def words_to_text(words: list[dict]) -> str:
    """Rebuild the plain transcript text from Scribe-shaped words."""
    return " ".join(w["text"].strip() for w in words if w.get("type") == "word").strip()
