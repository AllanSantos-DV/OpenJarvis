"""vox-engine speech backends -- local STT and TTS over a single shared daemon.

`vox-engine <https://github.com/AllanSantos-DV/vox-engine>`_ is a standalone
Windows daemon that keeps **one** Whisper model resident on the GPU (CPU
fallback) and serves both transcription and speech synthesis to every app on the
machine through a named pipe, with multi-session queueing.

Using it here replaces two cloud/local dependencies at once:

* STT -- instead of ``faster-whisper`` loading a second copy of Whisper into this
  process, transcription is delegated to the already-running daemon.
* TTS -- instead of ElevenLabs/OpenAI/Cartesia (paid, cloud), synthesis uses the
  daemon's local voices, which include native pt-BR ones.

Both backends are *reuse-if-running*: :func:`ensure_daemon` returns a live client
when the daemon is up and ``None`` otherwise, so they degrade to "unavailable"
rather than starting a competing engine.
"""

from __future__ import annotations

import logging
from typing import Any, List, Optional

from openjarvis.core.registry import SpeechRegistry, TTSRegistry
from openjarvis.speech.tts import TTSBackend, TTSResult

logger = logging.getLogger(__name__)

#: The daemon streams float32 PCM at this rate.
_SAMPLE_RATE = 24000


def _connect() -> Optional[Any]:
    """Return a live vox-engine client, or ``None`` when unavailable.

    ``ensure_daemon`` reuses a running daemon, starts an installed one, and
    returns ``None`` if neither is possible. Import errors are treated the same
    way, so a machine without vox-engine simply reports the backend as
    unavailable instead of breaking discovery.
    """
    try:
        from vox_engine.bootstrap import ensure_daemon
    except ImportError:
        logger.debug("vox-engine client not installed")
        return None

    try:
        return ensure_daemon()
    except Exception as exc:  # noqa: BLE001 -- discovery must never hard-fail
        logger.debug("vox-engine daemon unavailable: %s", exc)
        return None


class _VoxDaemonMixin:
    """Shared lazy-connect/health logic for the two vox-engine backends."""

    def __init__(self, *, session: str = "openjarvis") -> None:
        self._session = session
        self._client: Optional[Any] = None

    def _ensure_client(self) -> Any:
        if self._client is None:
            self._client = _connect()
        if self._client is None:
            raise RuntimeError(
                "vox-engine daemon is not available. Install it from "
                "https://github.com/AllanSantos-DV/vox-engine and make sure the "
                "daemon is running (it starts with Windows)."
            )
        return self._client

    def _info(self) -> dict:
        """Daemon status dict, or ``{}`` when it cannot be reached."""
        try:
            return self._ensure_client().info()
        except Exception as exc:  # noqa: BLE001
            logger.debug("vox-engine info() failed: %s", exc)
            return {}

    def close(self) -> None:
        """Release the pipe handle (idempotent)."""
        client, self._client = self._client, None
        if client is not None:
            try:
                client.close()
            except Exception:  # noqa: BLE001
                pass


@SpeechRegistry.register("vox-engine")
class VoxEngineSpeechBackend(_VoxDaemonMixin):
    """Speech-to-text backend delegating to the vox-engine daemon."""

    backend_id = "vox-engine"

    def __init__(self, *, language: str = "", session: str = "openjarvis") -> None:
        super().__init__(session=session)
        self._language = language

    def transcribe(
        self,
        audio: Any,
        *,
        language: str = "",
        **kwargs: Any,
    ) -> dict:
        """Transcribe float32 PCM samples and return ``{"text", "segments", ...}``.

        Uses ``transcribe_file`` so recordings longer than Whisper's ~30s window
        are segmented by the daemon instead of being silently truncated.
        """
        client = self._ensure_client()
        return client.transcribe_file(
            audio,
            lang=language or self._language,
            session=self._session,
            **kwargs,
        )

    def health(self) -> bool:
        """True when the daemon is reachable and its STT model is loaded."""
        return bool(self._info().get("stt_ready"))


@TTSRegistry.register("vox-engine")
class VoxEngineTTSBackend(_VoxDaemonMixin, TTSBackend):
    """Text-to-speech backend delegating to the vox-engine daemon."""

    backend_id = "vox-engine"

    def __init__(self, *, voice: str = "", session: str = "openjarvis") -> None:
        super().__init__(session=session)
        self._voice = voice

    def synthesize(
        self,
        text: str,
        *,
        voice_id: str = "",
        speed: float = 1.0,
        output_format: str = "wav",
    ) -> TTSResult:
        """Synthesize *text* and return encoded audio bytes.

        ``TTSResult.audio`` is declared as ``bytes``, so a compressed format is
        requested from the daemon (``wav``/``mp3``/``opus``); asking for ``pcm``
        would yield a numpy array and break that contract.
        """
        client = self._ensure_client()
        fmt = (output_format or "wav").lower()
        if fmt == "pcm":
            fmt = "wav"

        header, audio = client.tts(
            text,
            voice=voice_id or self._voice or None,
            speed=speed,
            session=self._session,
            fmt=fmt,
        )

        if not isinstance(audio, (bytes, bytearray)):
            raise RuntimeError(
                f"vox-engine returned {type(audio).__name__} for format {fmt!r}; "
                "expected encoded bytes."
            )

        sample_rate = int(header.get("sample_rate") or _SAMPLE_RATE)
        return TTSResult(
            audio=bytes(audio),
            format=str(header.get("format") or fmt),
            voice_id=str(header.get("voice") or voice_id or self._voice),
            sample_rate=sample_rate,
            duration_seconds=float(header.get("duration") or 0.0),
            metadata={"backend": self.backend_id, "codec": header.get("codec", "")},
        )

    def available_voices(self) -> List[str]:
        """Voice names reported by the daemon (never hardcoded)."""
        voices = self._info().get("tts_voices") or []
        return [
            str(v.get("name", "")) if isinstance(v, dict) else str(v)
            for v in voices
            if v
        ]

    def default_voice(self) -> str:
        """The daemon's configured default voice, or ``""``."""
        return str(self._info().get("default_voice") or "")

    def health(self) -> bool:
        """True when the daemon is reachable and its TTS model is loaded."""
        return bool(self._info().get("tts_ready"))


__all__ = ["VoxEngineSpeechBackend", "VoxEngineTTSBackend"]
