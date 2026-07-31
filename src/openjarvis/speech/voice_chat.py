"""Talk to Jarvis: press a key, speak, hear him answer.

The point of this file is to answer the one question that decides whether the
voice is worth building an app around: **is it good enough that the owner would
rather speak than type?** It needs no Rust, no WebView2 and no window -- if the
voice does not carry a conversation here, no amount of UI will save it.

Everything local: the microphone through vox-engine's Whisper, the answer
through the Copilot subscription, the speech through vox-engine's pt-BR voices.
Nothing on this path touches a cloud speech service or the GPU.

Push-to-talk on purpose. An always-listening microphone is exactly what the
owner said he did not want, and a wake word would be a second thing to get
wrong before the first one works.
"""

from __future__ import annotations

import io
import logging
import sys
import wave
from typing import Any, List, Optional

logger = logging.getLogger(__name__)

#: vox-engine's Whisper runs at 16 kHz mono; recording anything else means
#: resampling on the way in for no benefit.
SAMPLE_RATE = 16_000
CHANNELS = 1

#: Long enough for a real instruction, short enough that a stuck recording ends.
MAX_SECONDS = 60


class VoiceUnavailable(RuntimeError):
    """Raised when the pieces a voice conversation needs are not there."""


def _require_sounddevice():
    try:
        import sounddevice  # noqa: PLC0415
    except Exception as exc:  # noqa: BLE001
        raise VoiceUnavailable(
            "A captura de audio precisa do pacote sounddevice: "
            "uv pip install sounddevice"
        ) from exc
    return sounddevice


def _as_wav(samples: Any, sample_rate: int = SAMPLE_RATE) -> bytes:
    """Wrap int16 samples in a WAV container.

    The daemon takes encoded audio, not raw frames, and a bare buffer would be
    interpreted with whatever defaults it assumes -- the header is what makes
    the rate and width explicit.
    """
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as out:
        out.setnchannels(CHANNELS)
        out.setsampwidth(2)
        out.setframerate(sample_rate)
        out.writeframes(samples.tobytes())
    return buffer.getvalue()


class VoiceChat:
    """One push-to-talk conversation: listen, ask, speak, repeat."""

    def __init__(
        self,
        *,
        agent: Optional[Any] = None,
        stt: Optional[Any] = None,
        tts: Optional[Any] = None,
        voice: str = "",
        max_seconds: int = MAX_SECONDS,
    ) -> None:
        self._agent = agent
        self._stt = stt
        self._tts = tts
        self._voice = voice
        self._max_seconds = max_seconds
        self._history: List[str] = []

    # -- the pieces ----------------------------------------------------------

    def check(self) -> None:
        """Fail loudly and specifically before the first word is spoken.

        A voice loop that dies mid-sentence with a stack trace is worse than one
        that refuses to start: the owner is left wondering whether he mumbled.
        """
        stt, tts = self._speech()
        missing = []
        if not stt.health():
            missing.append("reconhecimento de fala (STT)")
        if not tts.health():
            missing.append("sintese de voz (TTS)")
        if missing:
            raise VoiceUnavailable(
                f"vox-engine esta sem: {', '.join(missing)}. "
                "Abra o vox-engine e espere ele carregar os modelos."
            )
        _require_sounddevice()

    def listen(self) -> str:
        """Record until Enter, then transcribe. Returns the text heard."""
        sd = _require_sounddevice()
        frames: List[Any] = []

        with sd.InputStream(
            samplerate=SAMPLE_RATE,
            channels=CHANNELS,
            dtype="int16",
            callback=lambda data, *_: frames.append(data.copy()),
        ):
            input()  # the owner presses Enter to stop talking

        if not frames:
            return ""

        import numpy  # noqa: PLC0415 -- only needed once audio actually arrived

        audio = numpy.concatenate(frames, axis=0)
        stt, _ = self._speech()
        result = stt.transcribe(_as_wav(audio))
        return str(result.get("text") or "").strip()

    def ask(self, question: str) -> str:
        """Put the question to Jarvis, carrying the conversation so far."""
        agent = self._agent or self._default_agent()
        result = agent.run(question)
        if result.metadata.get("error"):
            raise RuntimeError(result.content)
        return (result.content or "").strip()

    def say(self, text: str) -> None:
        """Speak *text* out loud through vox-engine."""
        if not text:
            return
        _, tts = self._speech()
        audio = tts.synthesize(text, voice_id=self._voice, output_format="wav")
        self._play(audio.audio)

    # -- the loop ------------------------------------------------------------

    def run(self, *, greeting: str = "") -> None:
        """Converse until the owner says goodbye or hits Ctrl+C."""
        self.check()
        if greeting:
            print(f"\n  Jarvis: {greeting}")
            self.say(greeting)

        while True:
            print("\n  [Enter] para falar, ou 'sair'...", end=" ", flush=True)
            command = input().strip().lower()
            if command in {"sair", "tchau", "fim", "exit", "quit"}:
                self.say("Ate mais.")
                return

            print("  ouvindo... (Enter para parar)", flush=True)
            try:
                heard = self.listen()
            except KeyboardInterrupt:
                return
            if not heard:
                print("  nao ouvi nada.")
                continue

            print(f"  Voce: {heard}")
            try:
                answer = self.ask(heard)
            except Exception as exc:  # noqa: BLE001
                # Say the failure out loud too. In a voice conversation a silent
                # error is indistinguishable from the assistant ignoring you.
                message = f"Deu erro: {exc}"
                print(f"  {message}")
                self.say("Deu erro aqui, da uma olhada no terminal.")
                continue

            print(f"  Jarvis: {answer}")
            self.say(answer)

    # -- seams ---------------------------------------------------------------

    def _speech(self):
        if self._stt is None or self._tts is None:
            from openjarvis.speech.vox_engine import (
                VoxEngineSpeechBackend,
                VoxEngineTTSBackend,
            )

            self._stt = self._stt or VoxEngineSpeechBackend()
            self._tts = self._tts or VoxEngineTTSBackend()
        return self._stt, self._tts

    def _default_agent(self):
        from openjarvis.agents.copilot_cli import CopilotCliAgent
        from openjarvis.conductor.adapters import UNATTENDED_ENV

        # One agent for the whole conversation: it keeps its CLI session, so
        # each answer has the earlier turns behind it.
        #
        # UNATTENDED_ENV matters here: a child `copilot` inherits this machine's
        # plugins, and the voice-chat hook blocks any turn that does not call
        # its `falar` tool -- which a headless child does not have. Measured:
        # without it the answer comes back EMPTY and no credits are spent, so it
        # looks like the model had nothing to say. Jarvis does its own speaking.
        self._agent = CopilotCliAgent(
            None, "auto", timeout=300, sandboxed=False, env=dict(UNATTENDED_ENV)
        )
        return self._agent

    def _play(self, audio: bytes) -> None:
        sd = _require_sounddevice()
        import numpy  # noqa: PLC0415

        with wave.open(io.BytesIO(audio), "rb") as source:
            rate = source.getframerate()
            frames = source.readframes(source.getnframes())

        sd.play(numpy.frombuffer(frames, dtype=numpy.int16), rate)
        sd.wait()


def main(argv: Optional[List[str]] = None) -> int:
    """Entry point: ``python -m openjarvis.speech.voice_chat``."""
    logging.basicConfig(level=logging.WARNING)
    chat = VoiceChat()
    try:
        chat.run(greeting="Oi Allan, pode falar.")
    except VoiceUnavailable as exc:
        print(f"\n  {exc}\n", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
