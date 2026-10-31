"""Speech-to-text providers for video-use.

    VIDEO_USE_STT=fcp|scribe|auto   (default auto)
    VIDEO_USE_STT_PREFER=scribe     (auto picks Scribe even when FCP is up)

`auto` picks Final Cut Pro's native transcriber when the SpliceKit bridge
answers, else ElevenLabs Scribe when an API key is found, else fails with
both fixes named.
"""
from __future__ import annotations

import os
import sys

from fcp.bridge import Bridge

from .base import Provider
from .fcp import FcpProvider
from .scribe import ScribeProvider, find_api_key

CHOICES = ("auto", "fcp", "scribe")

__all__ = ["CHOICES", "FcpProvider", "Provider", "ScribeProvider", "find_api_key",
           "resolve_provider"]


def resolve_provider(name: str | None = None, *, bridge: Bridge | None = None) -> Provider:
    """Pick the STT provider from `name` or $VIDEO_USE_STT and announce it on stderr."""
    choice = (name or os.environ.get("VIDEO_USE_STT") or "auto").strip().lower()
    if choice not in CHOICES:
        raise ValueError(f"unknown STT provider {choice!r}; expected one of {', '.join(CHOICES)}")

    provider: Provider
    if choice == "fcp":
        provider = FcpProvider(bridge)
    elif choice == "scribe":
        provider = ScribeProvider()
    else:
        b = bridge or Bridge()
        prefer_scribe = os.environ.get("VIDEO_USE_STT_PREFER", "").strip().lower() == "scribe"
        key = find_api_key()
        if not prefer_scribe and b.alive():
            provider = FcpProvider(b)
        elif key:
            provider = ScribeProvider(key)
        elif prefer_scribe and b.alive():
            provider = FcpProvider(b)
        else:
            raise RuntimeError(
                "no speech-to-text provider available: open Final Cut Pro with SpliceKit "
                "injected (bridge on 127.0.0.1:9876), or set ELEVENLABS_API_KEY in .env"
            )
    print(f"stt provider: {provider.name}", file=sys.stderr, flush=True)
    return provider
