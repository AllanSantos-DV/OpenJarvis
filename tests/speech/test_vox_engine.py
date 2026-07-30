"""Unit tests for the vox-engine speech/TTS backends (no daemon required)."""

from __future__ import annotations

import pytest

from openjarvis.core.registry import SpeechRegistry, TTSRegistry
from openjarvis.speech.vox_engine import (
    VoxEngineSpeechBackend,
    VoxEngineTTSBackend,
)


@pytest.fixture(autouse=True)
def _register_vox_engine():
    """Re-register after any registry clear."""
    if not SpeechRegistry.contains("vox-engine"):
        SpeechRegistry.register_value("vox-engine", VoxEngineSpeechBackend)
    if not TTSRegistry.contains("vox-engine"):
        TTSRegistry.register_value("vox-engine", VoxEngineTTSBackend)


class _FakeClient:
    """Stand-in for ``VoxClient`` that records calls."""

    def __init__(self, info=None, tts_result=None):
        self._info = info or {}
        self._tts_result = tts_result
        self.calls = []
        self.closed = False

    def info(self):
        return self._info

    def transcribe_file(self, audio, **kwargs):
        self.calls.append(("transcribe_file", kwargs))
        return {"text": "ok", "segments": []}

    def tts(self, text, **kwargs):
        self.calls.append(("tts", {"text": text, **kwargs}))
        return self._tts_result

    def close(self):
        self.closed = True


def _attach(backend, client):
    backend._client = client
    return backend


def test_backends_are_registered():
    assert SpeechRegistry.contains("vox-engine")
    assert TTSRegistry.contains("vox-engine")
    assert SpeechRegistry.get("vox-engine") is VoxEngineSpeechBackend
    assert TTSRegistry.get("vox-engine") is VoxEngineTTSBackend


def test_health_reflects_daemon_readiness():
    stt = _attach(VoxEngineSpeechBackend(), _FakeClient({"stt_ready": True}))
    tts = _attach(VoxEngineTTSBackend(), _FakeClient({"tts_ready": False}))

    assert stt.health() is True
    assert tts.health() is False


def test_health_is_false_when_daemon_is_missing(monkeypatch):
    monkeypatch.setattr("openjarvis.speech.vox_engine._connect", lambda: None)
    assert VoxEngineSpeechBackend().health() is False
    assert VoxEngineTTSBackend().health() is False


def test_available_voices_reads_daemon_not_hardcoded():
    info = {
        "tts_voices": [
            {"name": "vits-piper-pt_BR-faber-medium", "lang": "pt"},
            {"name": "kokoro-int8-multi-lang-v1_1", "lang": "multi"},
        ],
        "default_voice": "vits-piper-pt_BR-faber-medium",
    }
    tts = _attach(VoxEngineTTSBackend(), _FakeClient(info))

    assert tts.available_voices() == [
        "vits-piper-pt_BR-faber-medium",
        "kokoro-int8-multi-lang-v1_1",
    ]
    assert tts.default_voice() == "vits-piper-pt_BR-faber-medium"


def test_available_voices_empty_when_daemon_unreachable(monkeypatch):
    monkeypatch.setattr("openjarvis.speech.vox_engine._connect", lambda: None)
    assert VoxEngineTTSBackend().available_voices() == []


def test_synthesize_returns_encoded_bytes():
    header = {"format": "wav", "sample_rate": 22050, "voice": "v1", "duration": 1.5}
    client = _FakeClient(tts_result=(header, b"RIFFfake"))
    tts = _attach(VoxEngineTTSBackend(), client)

    result = tts.synthesize("olá", voice_id="v1", output_format="wav")

    assert result.audio == b"RIFFfake"
    assert result.format == "wav"
    assert result.sample_rate == 22050
    assert result.voice_id == "v1"
    assert result.duration_seconds == 1.5


def test_synthesize_upgrades_pcm_to_wav():
    """``TTSResult.audio`` is bytes, so raw PCM (numpy) must not be requested."""
    client = _FakeClient(tts_result=({"format": "wav"}, b"RIFF"))
    tts = _attach(VoxEngineTTSBackend(), client)

    tts.synthesize("oi", output_format="pcm")

    _, kwargs = client.calls[0]
    assert kwargs["fmt"] == "wav"


def test_synthesize_rejects_non_bytes_payload():
    client = _FakeClient(tts_result=({"format": "pcm"}, [0.1, 0.2]))
    tts = _attach(VoxEngineTTSBackend(), client)

    with pytest.raises(RuntimeError, match="expected encoded bytes"):
        tts.synthesize("oi")


def test_transcribe_uses_transcribe_file_for_long_audio():
    """``transcribe_file`` avoids Whisper's ~30s truncation window."""
    client = _FakeClient()
    stt = _attach(VoxEngineSpeechBackend(), client)

    stt.transcribe([0.0], language="pt")

    name, kwargs = client.calls[0]
    assert name == "transcribe_file"
    assert kwargs["lang"] == "pt"


def test_missing_daemon_raises_actionable_error(monkeypatch):
    monkeypatch.setattr("openjarvis.speech.vox_engine._connect", lambda: None)

    with pytest.raises(RuntimeError, match="vox-engine daemon is not available"):
        VoxEngineTTSBackend().synthesize("oi")


def test_close_is_idempotent():
    client = _FakeClient()
    tts = _attach(VoxEngineTTSBackend(), client)

    tts.close()
    tts.close()

    assert client.closed is True
