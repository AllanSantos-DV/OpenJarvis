"""vox-engine speech backends -- local STT and TTS over a single shared daemon.

`vox-engine <https://github.com/AllanSantos-DV/vox-engine>`_ is a standalone
Windows daemon that keeps **one** Whisper model resident on the GPU (CPU
fallback) and serves both transcription and speech synthesis to every app on the
machine through a named pipe, with multi-session queueing.

Using it here replaces two dependencies at once:

* STT -- instead of ``faster-whisper`` loading a second copy of Whisper into this
  process, transcription is delegated to the already-running daemon.
* TTS -- instead of ElevenLabs/OpenAI/Cartesia (paid, cloud), synthesis uses the
  daemon's local voices, which include native pt-BR ones.

Integration goes through the engine's own **vendored SDK** (``_vendor/``), which
is its supported entry point: the SDK connects to a live daemon, or installs and
updates the engine from a signed release (Ed25519, fail-closed) and boots it,
then hands back a ready client. It is stdlib-only -- no ``pywin32``, no ``numpy``,
and notably no import of ``vox_engine`` itself -- so the pipe address, the port
behind it and the update policy stay the engine's business, not ours.

The vendored files are byte-identical copies of the canonical SDK (``sdk/python``)
so upstream's drift check keeps working; they are never patched here.
"""

from __future__ import annotations

import importlib
import importlib.util
import logging
import sys
from pathlib import Path
from typing import Any, List, Optional

from openjarvis.core.registry import SpeechRegistry, TTSRegistry
from openjarvis.speech._stubs import SpeechBackend, TranscriptionResult
from openjarvis.speech.tts import TTSBackend, TTSResult

logger = logging.getLogger(__name__)

_VENDOR_DIR = Path(__file__).resolve().parent / "_vendor"

#: Fallback sample rate when the daemon does not report one.
_SAMPLE_RATE = 24000


#: sha256 of each vendored SDK file, over LF-normalised bytes (Git rewrites line
#: endings on checkout, and CRLF/LF is not drift). Checked at import, not only
#: under pytest: a suite that catches this on someone's laptop does nothing for
#: a process that boots from a modified copy and speaks to the owner's
#: microphone. Regenerate deliberately when refreshing the SDK, and read the
#: diff first -- nobody edits a vendored file on purpose.
_VENDOR_SHA256 = {
    "vox_sdk.py": "d0249e8c0b6809901c91b58379436e86dc89319c551c3102df804143095349ad",
    "vox_lifecycle.py": (
        "12e618b92a759118548717c43d6f1ed737f6bd238f5d7f917f84fd76f943860c"
    ),
    "_ed25519_ref.py": (
        "f527e89be8a4c9c95aad4b096f762afb39c64e9a645156946d7b2e2c11187bda"
    ),
}


class VendoredSdkTampered(RuntimeError):
    """The vendored SDK on disk is not the copy that was reviewed."""


def _verify_vendored(path: Path) -> None:
    """Refuse to load a vendored file that does not match its recorded hash.

    Loud on purpose. This code talks to a daemon that holds a microphone; a
    modified copy is either an accident nobody noticed or something worse, and
    both deserve to stop the process rather than a line in a log. An unknown
    file (not in the table) is also refused -- an unreviewed file is exactly
    what this is meant to catch.
    """
    import hashlib

    expected = _VENDOR_SHA256.get(path.name)
    if expected is None:
        raise VendoredSdkTampered(
            f"{path.name} nao esta na lista de arquivos vendorizados conhecidos."
        )
    actual = hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()
    if actual != expected:
        raise VendoredSdkTampered(
            f"{path.name} difere da copia revisada.\n"
            f"  esperado: {expected}\n"
            f"  no disco: {actual}\n"
            "Ninguem edita um arquivo vendorizado de proposito: ou algo foi "
            "alterado por acidente, ou o SDK foi atualizado. Se foi atualizacao, "
            "leia o diff e atualize _VENDOR_SHA256 no mesmo commit."
        )


def _load_sdk() -> Optional[Any]:
    """Return the vox-SDK lifecycle module, or ``None`` when unavailable.

    An installed ``vox_lifecycle`` wins over the vendored copy, so a machine that
    tracks the SDK through its own package manager is not pinned to whichever
    version happens to be vendored here.

    The vendored copy is registered in ``sys.modules`` under the SDK's canonical
    names because ``vox_lifecycle`` imports ``vox_sdk`` absolutely; rewriting that
    import would break upstream's byte-identity check on vendored copies.
    """
    try:
        return importlib.import_module("vox_lifecycle")
    except ImportError:
        pass

    for name in ("vox_sdk", "vox_lifecycle"):
        if name in sys.modules:
            continue
        path = _VENDOR_DIR / f"{name}.py"
        if not path.exists():
            logger.debug("vendored vox-SDK missing: %s", path)
            return None
        _verify_vendored(path)
        spec = importlib.util.spec_from_file_location(name, path)
        if spec is None or spec.loader is None:
            return None
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        try:
            spec.loader.exec_module(module)
        except Exception as exc:  # noqa: BLE001 -- never break discovery
            del sys.modules[name]
            logger.debug("failed to load vendored vox-SDK %s: %s", name, exc)
            return None

    return sys.modules.get("vox_lifecycle")


def _connect(*, autostart: bool) -> Optional[Any]:
    """Return a live vox-engine client, or ``None`` when unavailable.

    ``autostart=False`` is a cheap probe: it reports the engine as unavailable
    instead of installing it. That distinction matters during backend discovery,
    where a health check must never kick off a multi-minute install and first
    model load. ``autostart=True`` is used when the caller actually wants audio
    work done, and lets the SDK install, update and boot the engine as needed.
    """
    sdk = _load_sdk()
    if sdk is None:
        return None

    try:
        return sdk.ensure_vox(autostart=autostart)
    except Exception as exc:  # noqa: BLE001 -- discovery must never hard-fail
        logger.debug("vox-engine unavailable (autostart=%s): %s", autostart, exc)
        return None


class _VoxDaemonMixin:
    """Shared lazy-connect/health logic for the two vox-engine backends."""

    def __init__(self, *, session: str = "openjarvis") -> None:
        self._session = session
        self._client: Optional[Any] = None

    def _ensure_client(self) -> Any:
        """Client for real work; may install/boot the engine through the SDK."""
        if self._client is None:
            self._client = _connect(autostart=True)
        if self._client is None:
            raise RuntimeError(
                "vox-engine is not available. Install it from "
                "https://github.com/AllanSantos-DV/vox-engine (the SDK can also "
                "install it automatically from a signed release)."
            )
        return self._client

    def _probe(self) -> Optional[Any]:
        """Connect only if the engine is already up; never install."""
        if self._client is None:
            self._client = _connect(autostart=False)
        return self._client

    def _info(self) -> dict:
        """Daemon status dict, or ``{}`` when the engine is not running."""
        client = self._probe()
        if client is None:
            return {}
        try:
            return client.info()
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


#: Arguments ``VoxClient.transcribe_file`` accepts, besides the audio itself.
#: Derived from the vendored SDK signature; anything else a generic caller
#: passes (``format``, ``sample_rate``, ...) is dropped rather than forwarded.
_TRANSCRIBE_ARGS = frozenset(
    {"lang", "session", "priority", "profile", "model", "timeout"}
)


@SpeechRegistry.register("vox-engine")
class VoxEngineSpeechBackend(_VoxDaemonMixin, SpeechBackend):
    """Speech-to-text backend delegating to the vox-engine daemon."""

    backend_id = "vox-engine"

    def __init__(
        self,
        *,
        language: str = "",
        profile: str = "",
        session: str = "openjarvis",
    ) -> None:
        super().__init__(session=session)
        self._language = language
        self._profile = profile

    def transcribe(
        self,
        audio: Any,
        *,
        format: str = "wav",  # noqa: A002 -- the interface declares this name
        language: Optional[str] = None,
        profile: str = "",
        **kwargs: Any,
    ) -> TranscriptionResult:
        """Transcribe audio and return a :class:`TranscriptionResult`.

        Uses the SDK's ``transcribe_file`` track so recordings longer than
        Whisper's ~30s window are segmented by the daemon instead of being
        truncated. Without an explicit *profile* the engine picks the fast
        ``transcription`` one; pass ``transcription_hq`` for difficult audio.

        This signature is the one :class:`SpeechBackend` declares, and getting
        it wrong cost the owner a live failure: the class did not inherit the
        ABC, so nothing checked it. ``format`` fell into ``**kwargs`` and was
        forwarded to an SDK that has no such argument, and the endpoint's
        ``result.text`` met a plain dict. Both surfaced as one opaque
        "Speech transcription failed (500)".

        ``format`` is accepted and ignored on purpose: the daemon sniffs the
        container itself, so the hint is genuinely unnecessary here -- but
        declaring it is what keeps a caller honouring the interface from
        breaking. Any other unexpected argument is dropped and logged rather
        than forwarded.
        """
        client = self._ensure_client()
        chosen = profile or self._profile
        if chosen:
            kwargs["profile"] = chosen

        accepted = {k: v for k, v in kwargs.items() if k in _TRANSCRIBE_ARGS}
        ignored = sorted(set(kwargs) - set(accepted))
        if ignored:
            logger.debug("vox-engine ignores unsupported transcribe args: %s", ignored)

        text = client.transcribe_file(
            audio,
            lang=language or self._language,
            session=self._session,
            **accepted,
        )
        return TranscriptionResult(
            text=text,
            language=language or self._language or None,
        )

    def supported_formats(self) -> List[str]:
        """Containers the daemon accepts.

        Part of the interface, and it was missing entirely -- which is the
        clearest evidence that this class never really implemented it. The
        daemon reads the container itself, so this is what it decodes rather
        than a filter applied here.
        """
        return ["wav", "mp3", "flac", "ogg", "opus", "m4a", "webm"]

    def health(self) -> bool:
        """True when the engine is already running with its STT model loaded."""
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
        would yield float32 samples and break that contract.
        """
        client = self._ensure_client()
        fmt = (output_format or "wav").lower()
        if fmt == "pcm":
            fmt = "wav"

        header, audio = client.tts(
            text,
            fmt=fmt,
            voice=voice_id or self._voice or None,
            speed=speed,
            session=self._session,
        )

        if not isinstance(audio, (bytes, bytearray)):
            raise RuntimeError(
                f"vox-engine returned {type(audio).__name__} for format {fmt!r}; "
                "expected encoded bytes."
            )

        return TTSResult(
            audio=bytes(audio),
            format=str(header.get("format") or fmt),
            voice_id=str(header.get("voice") or voice_id or self._voice),
            sample_rate=int(header.get("sample_rate") or _SAMPLE_RATE),
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
        """True when the engine is already running with its TTS model loaded."""
        return bool(self._info().get("tts_ready"))


__all__ = ["VoxEngineSpeechBackend", "VoxEngineTTSBackend"]
