"""Transcription against the REAL vox-engine daemon.

Every other test here drives ``_FakeClient``, which accepts any keyword and
returns a canned string. That is why a mocked suite stayed green while the live
call raised ``unexpected keyword argument 'format'`` and, one layer down, the
endpoint met a dict where it wanted a dataclass. A stand-in that says yes to
everything cannot fail the way the real thing fails.

So this speaks to the daemon: real audio in, real text out, through the same
call the HTTP endpoint makes. It skips when the engine is not running, because a
test that cannot run should say so rather than pass.
"""

from __future__ import annotations

import io
import wave

import pytest

from openjarvis.speech._stubs import TranscriptionResult
from openjarvis.speech.vox_engine import VoxEngineSpeechBackend


def _tone_wav(seconds: float = 1.0, rate: int = 16_000) -> bytes:
    """A short WAV. The content does not matter -- reaching the daemon does."""
    import numpy

    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(rate)
        samples = numpy.linspace(0, seconds, int(rate * seconds))
        wave_data = numpy.sin(2 * numpy.pi * 220 * samples) * 8000
        out.writeframes(wave_data.astype(numpy.int16).tobytes())
    return buffer.getvalue()


@pytest.fixture
def daemon():
    backend = VoxEngineSpeechBackend()
    try:
        ready = backend.health()
    except Exception:  # noqa: BLE001 -- not installed here
        ready = False
    if not ready:
        pytest.skip("vox-engine is not running with its STT model loaded")
    return backend


@pytest.mark.contract
def test_the_real_daemon_answers_the_declared_interface(daemon):
    """The call the HTTP endpoint makes, against the engine that broke it.

    ``format`` and ``language`` are exactly what ``/v1/speech/transcribe``
    passes. Both were wrong before: one blew up inside the SDK, the other met a
    return type the caller could not read.
    """
    result = daemon.transcribe(_tone_wav(), format="wav", language="pt")

    assert isinstance(result, TranscriptionResult), (
        "the endpoint does result.text -- a dict silently 500s one line later"
    )
    assert isinstance(result.text, str)
    assert result.language == "pt"


@pytest.mark.contract
def test_the_real_daemon_accepts_a_profile(daemon):
    """The one optional argument this backend forwards on purpose."""
    result = daemon.transcribe(
        _tone_wav(), format="wav", language="pt", profile="transcription"
    )

    assert isinstance(result, TranscriptionResult)


@pytest.mark.contract
def test_an_unknown_argument_does_not_reach_the_real_sdk(daemon):
    """Proof the filter works where it matters, not just against a fake.

    A caller from the shared speech interface may pass anything its own backend
    understood. Before the filter this raised inside the SDK.
    """
    result = daemon.transcribe(
        _tone_wav(), format="wav", language="pt", sample_rate=16_000, verbose=True
    )

    assert isinstance(result, TranscriptionResult)
