"""The speech seam for Workspace Direct calls (trinity-enterprise#1046).

Speech-to-text and text-to-speech behind one interface, with no conversational
model in either direction. A bridge takes a `SpeechProvider` instance, so a caller
that holds its own credential can construct one and pass it in.
"""
from .base import SpeechError, SpeechProvider, Transcript
from .registry import direct_voice_readiness, resolve_direct_provider

__all__ = [
    "SpeechError",
    "SpeechProvider",
    "Transcript",
    "direct_voice_readiness",
    "resolve_direct_provider",
]
