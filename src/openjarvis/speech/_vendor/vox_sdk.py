"""vox-SDK (Python) — bootstrap standalone do motor de voz ``vox-engine``.

Um cliente que AUTO-INSTALA integra com UMA chamada via o add-on ``vox_lifecycle.py``
(vendorizado ao lado); um cliente REMOTO que só fala com um daemon no ar usa ``VoxClient``
diretamente e dispensa o add-on (veja "Vendoring scopes" no README)::

    from vox_lifecycle import ensure_vox
    client = ensure_vox()          # instala/atualiza/usa; devolve o cliente ou None
    if client:
        print(client.info())

Este módulo é **drop-in** e **stdlib-puro** (mais o verificador Ed25519 vendorizado
``_ed25519_ref.py``, que deve ficar ao lado deste arquivo). Ele **NÃO importa**
``vox_engine``, **não** exige ``pywin32`` nem ``cryptography`` e fala com o daemon
pelo named pipe usando ``open(pipe, "r+b", buffering=0)`` puro. ``truststore`` e
``numpy`` são opcionais (carregados sob ``try/except``).

Postura de segurança — **fail-closed**: o instalador da release só roda depois de
uma verificação Ed25519 (hash-then-sign sobre o SHA-256 do zip) bem-sucedida contra
o KEYRING embutido. Chave malformada/ausente, ``.sig`` ausente, erro de rede, ou
verificador indisponível ⇒ verificação falsa ⇒ o ``install.ps1`` NUNCA é executado.

O ``VoxClient`` aqui é **síncrono e serializado** (1 request em voo por vez): ele
espelha a ``_VoxBridge`` do voice-chat (uma request → uma resposta, protegida por um
lock, com thread leitora + timeout). NÃO é o :class:`vox_engine.client.pipe_client.VoxClient`
concorrente (multi-req roteado por ``req_id``). Para um único consumidor sequencial
(ditado, TTS on-demand) isto é suficiente e mais simples.

Fonte canônica: ``vox-engine/sdk/python/vox_sdk.py``. Clientes VENDORIZAM uma cópia
byte-idêntica (a mesma convenção de lock/config vale para o SDK Node irmão).
"""
# INVARIANT: core NEVER imports lifecycle — unidirectional by design (lifecycle imports core).
from __future__ import annotations

import json
import struct
import sys
import threading
import time
from typing import Literal

# ---------------------------------------------------------------------------
# CONFIG canônica — fonte única (os MESMOS valores no SDK Node irmão).
# ---------------------------------------------------------------------------
SDK_VERSION = "2.0.1"

DEFAULT_PIPE = r"\\.\pipe\vox"

# Tetos de sanidade do framing (espelham daemon/protocol.py).
MAX_JSON = 4 * 1024 * 1024
MAX_AUDIO = 512 * 1024 * 1024

# ---------------------------------------------------------------------------
# SUPERFÍCIE DE CAPACIDADES — tipos permitidos que o cliente importa (autocomplete)
# em vez de garimpar no código-fonte. Estas constantes ESPELHAM o daemon
# (``vox_engine.core.profiles``/``hardware``/``audio_encode`` + ``daemon.inference``);
# o teste anti-drift (tests/test_sdk_capabilities_parity.py) TRAVA a paridade — se o
# daemon mudar e o SDK não, o CI quebra. Para o estado REAL desta máquina em runtime
# (perfil→modelo resolvido, formatos servíveis, vozes), use ``VoxClient.capabilities()``.
# ---------------------------------------------------------------------------
ProfileName = Literal["dictation", "translator", "transcription", "transcription_hq"]
ModelName = Literal["base", "small", "turbo", "large-v3"]
Priority = Literal["interactive", "batch"]
AudioFormat = Literal["pcm", "wav", "opus", "vorbis", "mp3"]

PROFILES: tuple[str, ...] = ("dictation", "translator", "transcription", "transcription_hq")
PROFILE_PURPOSE: dict[str, str] = {
    "dictation": "ditado / trecho curto — velocidade (streaming)",
    "translator": "tradução em tempo real — único perfil com large-v3 (GPU)",
    "transcription": "arquivo completo — rápido (padrão; mesmo motor do ditado)",
    "transcription_hq": "arquivo completo — turbo (large-v3 é reservado à tradução)",
}
MODELS: tuple[str, ...] = ("base", "small", "turbo", "large-v3")
PRIORITIES: tuple[str, ...] = ("interactive", "batch")
DEFAULT_PRIORITY = "interactive"
AUDIO_FORMATS: tuple[str, ...] = ("pcm", "wav", "opus", "vorbis", "mp3")
DEFAULT_AUDIO_FORMAT = "pcm"


class Capabilities:
    """Retrato TIPADO do que o motor oferece NESTA máquina — uma chamada
    (:meth:`VoxClient.capabilities`) e o cliente sabe tudo, sem hardcode. Combina o
    handshake em RUNTIME (``info``: perfil→modelo já resolvido pro hardware, modelos
    residentes/permitidos, formatos servíveis, vozes) com as listas ESTÁTICAS conhecidas
    do SDK (``*_known``/``priorities``) — o padrão de mercado (typed + discovery).

    Classe simples (NÃO ``@dataclass``) de propósito: o SDK é vendorável e pode ser
    carregado standalone via ``importlib`` sem registrar em ``sys.modules``; um
    ``@dataclass`` sob ``from __future__ import annotations`` resolve as anotações via
    ``sys.modules[__module__]`` e QUEBRA nesse cenário. Um ``__init__`` explícito é imune."""

    def __init__(self, *, version=None, provider=None, hardware=None, profiles=None,
                 models_resident=None, models_allowed=None, models_known=MODELS,
                 audio_formats_available=None, audio_formats_known=AUDIO_FORMATS,
                 priorities=PRIORITIES, voices=None, supported_voices=None,
                 default_voice=None):
        self.version = version
        self.provider = provider
        self.hardware = hardware
        self.profiles = profiles if profiles is not None else {}       # runtime: nome -> {model,...}
        self.models_resident = models_resident if models_resident is not None else []
        self.models_allowed = models_allowed if models_allowed is not None else []
        self.models_known = models_known                                # estático (superset)
        self.audio_formats_available = (audio_formats_available
                                        if audio_formats_available is not None else [])
        self.audio_formats_known = audio_formats_known                  # estático (superset)
        self.priorities = priorities                                    # estático
        self.voices = voices if voices is not None else []
        self.supported_voices = supported_voices if supported_voices is not None else []
        self.default_voice = default_voice

    def __repr__(self) -> str:
        return (f"Capabilities(version={self.version!r}, provider={self.provider!r}, "
                f"profiles={list(self.profiles)}, models_resident={self.models_resident})")


__all__ = [
    "SDK_VERSION", "DEFAULT_PIPE",
    "PROFILES", "PROFILE_PURPOSE", "MODELS", "PRIORITIES", "DEFAULT_PRIORITY",
    "AUDIO_FORMATS", "DEFAULT_AUDIO_FORMAT", "Capabilities",
    "ProfileName", "ModelName", "Priority", "AudioFormat",
    "VoxClient", "VoxEngineError", "ProtocolError",
    "CaptureError", "CaptureHandle",
    "encode_message", "read_message", "make_recv_exact",
]


# Retrocompat LOUD (PEP 562): os símbolos de ciclo de vida saíram para vox_lifecycle
# no SDK 2.0. NÃO re-exportamos (isso violaria o invariante core→lifecycle); em vez
# disso um acesso a um nome MOVIDO levanta ImportError claro apontando o novo lar.
_MOVED_TO_LIFECYCLE = frozenset({
    "PUBKEYS", "RELEASES_API", "TAG_PREFIX", "INSTALLER_ASSET", "SIG_ASSET",
    "INSTALL_ROOT", "INSTALLED_PYTHON", "INSTALLED_PYTHONW", "INSTALLED_DICTATE",
    "DAEMON_LOG", "DAEMON_BOOT_LOG", "LOCK_DIR", "STALE_MS", "ACQUIRE_TIMEOUT_MS", "POLL_MS",
    "CONNECT_TIMEOUT_MS", "BOOT_TIMEOUT_MS", "INSTALL_TIMEOUT_MS",
    "verify_installer", "parse_version", "is_newer", "latest_release", "installed_version",
    "download_and_run_installer", "check_and_update", "start_installed_daemon", "stop_daemon",
    "ensure_vox", "ensure_vox_detailed", "update_engine",
})


def __getattr__(name):
    if name in _MOVED_TO_LIFECYCLE:
        raise ImportError(
            f"'{name}' moved to vox_lifecycle in SDK 2.0 — use 'from vox_lifecycle import {name}' "
            f"and vendor vox_lifecycle.py alongside vox_sdk.py.")
    raise AttributeError(f"module 'vox_sdk' has no attribute {name!r}")


# ---------------------------------------------------------------------------
# Framing binário [u32 json_len][u32 audio_len][json][audio] — PARIDADE byte a
# byte com daemon/protocol.py (copiado, NÃO importado de vox_engine).
# ---------------------------------------------------------------------------
_HDR = struct.Struct(">II")


class ProtocolError(Exception):
    """Frame malformado ou fora dos limites de sanidade."""


def encode_message(header: dict, audio: bytes = b"") -> bytes:
    """Serializa um frame completo (idêntico a protocol.encode_message)."""
    jb = json.dumps(header, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(jb) > MAX_JSON:
        raise ProtocolError(f"json muito grande: {len(jb)} > {MAX_JSON}")
    if len(audio) > MAX_AUDIO:
        raise ProtocolError(f"audio muito grande: {len(audio)} > {MAX_AUDIO}")
    return _HDR.pack(len(jb), len(audio)) + jb + audio


def read_message(recv_exact) -> "tuple[dict, bytes]":
    """Lê um frame via ``recv_exact(n)``. Devolve ``(header, audio)``.

    Levanta :class:`ProtocolError` em frame malformado e :class:`EOFError` se o
    stream fechar no início do frame. Espelho de protocol.read_message.
    """
    head = recv_exact(_HDR.size)
    if not head:
        raise EOFError("stream fechado")
    if len(head) != _HDR.size:
        raise ProtocolError("cabeçalho incompleto")
    json_len, audio_len = _HDR.unpack(head)
    if json_len > MAX_JSON or json_len == 0:
        raise ProtocolError(f"json_len inválido: {json_len}")
    if audio_len > MAX_AUDIO:
        raise ProtocolError(f"audio_len inválido: {audio_len}")
    jb = recv_exact(json_len)
    if len(jb) != json_len:
        raise ProtocolError("json truncado")
    audio = recv_exact(audio_len) if audio_len else b""
    if len(audio) != audio_len:
        raise ProtocolError("audio truncado")
    try:
        header = json.loads(jb.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise ProtocolError(f"json inválido: {e}") from e
    if not isinstance(header, dict):
        raise ProtocolError("header não é objeto")
    return header, audio


def make_recv_exact(recv):
    """Adapta um ``recv(n)`` parcial num ``recv_exact(n)`` acumulador."""
    def recv_exact(n: int) -> bytes:
        if n == 0:
            return b""
        chunks = []
        got = 0
        while got < n:
            b = recv(n - got)
            if not b:
                return b"".join(chunks)
            chunks.append(b)
            got += len(b)
        return b"".join(chunks)
    return recv_exact


# numpy é OPCIONAL e LAZY: NÃO importa no topo (front-load) — só sob demanda, quando um
# consumidor DECODIFICA PCM. Assim um consumidor que só usa wav/transcrito (ex.: o worker fino do
# voice-chat) fica NUMPY-FREE no import → economia real de RAM por fork (numpy é o grosso do USS).
# Cacheado após a 1ª sondagem; devolve o módulo ou None se indisponível (Python sem wheels).
_np = None
_np_probed = False
_np_lock = threading.Lock()


def _numpy():
    """Importa numpy SOB DEMANDA (cacheado, thread-safe). None se indisponível — mantém numpy
    OPCIONAL. Double-checked locking: um 2º thread que vê ``_np_probed`` True enxerga ``_np`` já
    setado (a escrita de ``_np`` precede ``_np_probed=True`` sob o lock) — nunca um None espúrio."""
    global _np, _np_probed
    if not _np_probed:
        with _np_lock:
            if not _np_probed:
                try:  # pragma: no cover - depende do ambiente
                    import numpy as _mod
                except Exception:  # noqa: BLE001
                    _mod = None
                _np = _mod
                _np_probed = True
    return _np


def _pcm_reply(h: dict, audio: bytes):
    """Decodifica o payload PCM float32 LE em ndarray se numpy houver; senão devolve os bytes
    crus. numpy é carregado LAZY aqui — quem nunca chama isto nunca importa numpy."""
    np = _numpy()
    if np is not None:
        return h, (np.frombuffer(audio, dtype="<f4") if audio else np.zeros(0, np.float32))
    return h, audio


def _to_pcm_bytes(audio) -> bytes:
    """Converte ``audio`` para PCM float32 little-endian.

    Aceita ``bytes``/``bytearray`` (repassa), ``numpy.ndarray`` (se numpy houver)
    ou qualquer iterável de floats (fallback stdlib via ``array``)."""
    if isinstance(audio, (bytes, bytearray)):
        return bytes(audio)
    np = _numpy()
    if np is not None:
        try:
            return np.ascontiguousarray(audio, dtype="<f4").tobytes()
        except Exception:  # noqa: BLE001
            pass
    import array
    a = array.array("f", (float(x) for x in audio))
    if sys.byteorder != "little":
        a.byteswap()
    return a.tobytes()


# ---------------------------------------------------------------------------
# VoxClient SÍNCRONO (1 request em voo). Espelha _VoxBridge do voice-chat.
# ---------------------------------------------------------------------------
class VoxEngineError(RuntimeError):
    """Motor de voz indisponível ou falha de framing (erro ALTO, sem mudo)."""


class _PartialSend(Exception):
    """Frame enviado só EM PARTE antes de falhar: NÃO é repetível (bytes já
    chegaram ao motor -> reenviar corromperia o stream / duplicaria o pedido)."""

    def __init__(self, sent: int):
        super().__init__(f"envio parcial ({sent} bytes)")
        self.sent = sent


# ---------------------------------------------------------------------------
# Transporte de pipe OVERLAPPED (ctypes/kernel32) — SÓ para a captura (stream).
# ---------------------------------------------------------------------------
# Um handle SÍNCRONO (``open(pipe,"r+b")``) serializa TODA a I/O do handle: um
# ``ReadFile`` bloqueado (thread-leitora) trava um ``WriteFile`` concorrente (o
# ``close`` do consumidor) no MESMO handle -> deadlock. Por isso o daemon e o
# ``pipe_client`` interno usam ``FILE_FLAG_OVERLAPPED``. O ``VoxClient`` síncrono
# deste SDK é seguro só porque é estritamente serial (escreve-então-lê sob lock,
# sem thread-leitora persistente). A CAPTURA tem thread-leitora + escrita
# concorrente, então PRECISA de overlapped — aqui em stdlib puro (``ctypes``) para
# o SDK vendorável não exigir ``pywin32``. Só Windows (o transporte é named pipe).
_GENERIC_READ = 0x80000000
_GENERIC_WRITE = 0x40000000
_OPEN_EXISTING = 3
_FILE_FLAG_OVERLAPPED = 0x40000000
_PIPE_READMODE_BYTE = 0x00000000
_ERROR_IO_PENDING = 997
_ERROR_PIPE_BUSY = 231
_INVALID_HANDLE_VALUE = (1 << (8 * struct.calcsize("P"))) - 1  # (void*)-1, largura do ponteiro

_win_api_cache = None
_win_api_lock = threading.Lock()


def _win_api():
    """Configura (uma vez) os protótipos ``kernel32`` da captura. LAZY: só toca em ``WinDLL``
    quando uma captura é aberta — o import do SDK segue cross-platform e stdlib-puro. Devolve
    ``(kernel32, _OVERLAPPED, ctypes, wintypes)`` cacheado."""
    global _win_api_cache
    if _win_api_cache is not None:
        return _win_api_cache
    with _win_api_lock:
        if _win_api_cache is not None:
            return _win_api_cache
        import ctypes
        from ctypes import wintypes

        k = ctypes.WinDLL("kernel32", use_last_error=True)

        class _OVERLAPPED(ctypes.Structure):
            _fields_ = [("Internal", ctypes.c_void_p), ("InternalHigh", ctypes.c_void_p),
                        ("Offset", wintypes.DWORD), ("OffsetHigh", wintypes.DWORD),
                        ("hEvent", wintypes.HANDLE)]

        _lpovl = ctypes.POINTER(_OVERLAPPED)
        _lpdw = ctypes.POINTER(wintypes.DWORD)
        k.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                  ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
        k.CreateFileW.restype = wintypes.HANDLE
        k.CreateEventW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR]
        k.CreateEventW.restype = wintypes.HANDLE
        k.ReadFile.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD, _lpdw, _lpovl]
        k.ReadFile.restype = wintypes.BOOL
        k.WriteFile.argtypes = [wintypes.HANDLE, ctypes.c_char_p, wintypes.DWORD, _lpdw, _lpovl]
        k.WriteFile.restype = wintypes.BOOL
        k.GetOverlappedResult.argtypes = [wintypes.HANDLE, _lpovl, _lpdw, wintypes.BOOL]
        k.GetOverlappedResult.restype = wintypes.BOOL
        k.SetNamedPipeHandleState.argtypes = [wintypes.HANDLE, _lpdw, ctypes.c_void_p,
                                              ctypes.c_void_p]
        k.SetNamedPipeHandleState.restype = wintypes.BOOL
        k.WaitNamedPipeW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD]
        k.WaitNamedPipeW.restype = wintypes.BOOL
        k.CancelIoEx.argtypes = [wintypes.HANDLE, _lpovl]
        k.CancelIoEx.restype = wintypes.BOOL
        k.ResetEvent.argtypes = [wintypes.HANDLE]
        k.ResetEvent.restype = wintypes.BOOL
        k.CloseHandle.argtypes = [wintypes.HANDLE]
        k.CloseHandle.restype = wintypes.BOOL
        _win_api_cache = (k, _OVERLAPPED, ctypes, wintypes)
        return _win_api_cache


class _OverlappedPipe:
    """fh duck-typed (``read``/``write``/``flush``/``close``) sobre um named pipe com I/O
    OVERLAPPED — leitura (thread-leitora) e escrita (consumidor) CONCORRENTES no MESMO handle
    sem deadlock. Cada operação tem seu próprio ``OVERLAPPED``; a leitura e a escrita usam
    EVENTOS separados (um leitor, escritas serializadas pelo handle) — nunca colidem."""

    def __init__(self, pipe_name: str, connect_timeout: float = 5.0):
        self._k, self._OVL, self._ctypes, self._wintypes = _win_api()
        self._closed = False
        self._close_lock = threading.Lock()
        self._h = self._connect(pipe_name, connect_timeout)
        self._read_evt = self._k.CreateEventW(None, True, False, None)
        self._write_evt = self._k.CreateEventW(None, True, False, None)
        if not self._read_evt or not self._write_evt:
            # fail-loud: sem os eventos manuais a I/O overlapped concorrente (a razão desta classe)
            # degradaria em silêncio p/ sinalização no próprio handle. Só sob exaustão de recursos.
            err = self._ctypes.get_last_error()
            for evt in (self._read_evt, self._write_evt):
                if evt:
                    try:
                        self._k.CloseHandle(evt)
                    except Exception:  # noqa: BLE001
                        pass
            try:
                self._k.CloseHandle(self._h)
            except Exception:  # noqa: BLE001
                pass
            raise OSError(f"CreateEvent falhou na captura: Win32 {err}")

    def _connect(self, pipe_name: str, timeout: float):
        ct = self._ctypes
        deadline = time.time() + max(0.0, timeout)
        while True:
            h = self._k.CreateFileW(pipe_name, _GENERIC_READ | _GENERIC_WRITE, 0, None,
                                    _OPEN_EXISTING, _FILE_FLAG_OVERLAPPED, None)
            if h and h != _INVALID_HANDLE_VALUE:
                mode = self._wintypes.DWORD(_PIPE_READMODE_BYTE)
                self._k.SetNamedPipeHandleState(h, ct.byref(mode), None, None)
                return h
            err = ct.get_last_error()
            if time.time() >= deadline:
                raise OSError(f"não abriu o pipe de captura {pipe_name}: erro Win32 {err}")
            if err == _ERROR_PIPE_BUSY:
                self._k.WaitNamedPipeW(pipe_name, 500)   # aguarda uma instância livre
            else:
                time.sleep(0.05)                          # ainda não existe/transiente: retenta

    def read(self, n: int) -> bytes:
        h = self._h
        if n <= 0 or self._closed or h is None:
            return b""
        ct = self._ctypes
        buf = ct.create_string_buffer(n)
        ov = self._OVL()
        ov.hEvent = self._read_evt
        self._k.ResetEvent(self._read_evt)
        ok = self._k.ReadFile(h, buf, n, None, ct.byref(ov))
        if not ok and ct.get_last_error() != _ERROR_IO_PENDING:
            return b""                        # pipe quebrado/abortado -> EOF (a leitora nunca cai)
        nread = self._wintypes.DWORD(0)
        if not self._k.GetOverlappedResult(h, ct.byref(ov), ct.byref(nread), True):
            return b""
        return buf.raw[:nread.value]

    def write(self, data) -> int:
        ct = self._ctypes
        h = self._h
        if self._closed or h is None:
            raise OSError("WriteFile da captura: conexão fechada")   # fail-loud
        data = bytes(data)
        ov = self._OVL()
        ov.hEvent = self._write_evt
        self._k.ResetEvent(self._write_evt)
        ok = self._k.WriteFile(h, data, len(data), None, ct.byref(ov))
        if not ok:
            err = ct.get_last_error()
            if err != _ERROR_IO_PENDING:
                raise OSError(f"WriteFile da captura falhou: Win32 {err}")  # fail-loud
        nwritten = self._wintypes.DWORD(0)
        if not self._k.GetOverlappedResult(h, ct.byref(ov), ct.byref(nwritten), True):
            raise OSError(f"WriteFile da captura incompleto: Win32 {ct.get_last_error()}")
        return nwritten.value

    def flush(self) -> None:
        pass

    def close(self) -> None:
        with self._close_lock:
            if self._closed:
                return
            self._closed = True
            h, revt, wevt = self._h, self._read_evt, self._write_evt
            self._h = None                      # read/write posteriores desistem (sentinela) —
            #                                     nunca reusam um valor de handle já fechado
        try:
            self._k.CancelIoEx(h, None)         # destrava um ReadFile pendente de OUTRA thread
        except Exception:  # noqa: BLE001
            pass
        for evt in (revt, wevt):
            try:
                self._k.CloseHandle(evt)
            except Exception:  # noqa: BLE001
                pass
        try:
            self._k.CloseHandle(h)
        except Exception:  # noqa: BLE001
            pass


class CaptureError(VoxEngineError):
    """Falha TIPADA de captura — ``code`` estável para o cliente mapear em UI SEM parsear texto.
    Códigos do daemon: ``busy`` / ``mic_busy`` / ``already_open`` / ``mic_open_failed`` /
    ``capture_timeout`` / ``not_open`` / ``not_owner`` / ``capture_internal`` /
    ``capture_unavailable`` / ``no_session``; e o client-side ``pipe_broken`` (a conexão de
    captura caiu no meio)."""

    def __init__(self, code: str, message: str = "", owner: "str | None" = None):
        super().__init__(f"{code}: {message}" if message else code)
        self.code = code
        self.message = message
        # ``owner``: quando ``code=="mic_busy"``, o sid do processo que segura o mic.lock
        # (ex.: "vox-dictate") — o consumidor mapeia para um rótulo ("em uso pelo ditado").
        # None nos demais códigos ou quando o dono soltou o mic no instante da recusa.
        self.owner = owner


_cap_rid_lock = threading.Lock()
_cap_rid_n = 0


def _cap_rid() -> str:
    """req_id monotônico para frames de captura (conexão própria, independente do VoxClient)."""
    global _cap_rid_n
    with _cap_rid_lock:
        _cap_rid_n += 1
        return f"cap-{_cap_rid_n}"


class CaptureHandle:
    """Handle de UMA captura de sessão (mic → transcrição no daemon) sobre uma conexão de pipe
    DEDICADA. Ao contrário do :class:`VoxClient` (1 request → 1 resposta), a captura é um STREAM:
    o daemon empurra N frames assíncronos numa thread-leitora e CADA um é entregue a
    ``on_event(frame)`` — o único ``emit`` que o consumidor liga na sua ponte (callback→iterador).

    Formas do ``frame`` (discrimine por ``frame['event']``):
      * ``{event:"capture_segment", session, idx, text}``  — trecho transcrito (pipeline no daemon)
      * ``{event:"capture_level",   session, rms, peak, silent}`` — VU (~10 Hz); dica de exibição
      * ``{event:"capture_error",   session, code, message}``     — erro de device (``device_fail``)
        ou, client-side, ``code="pipe_broken"`` quando a conexão cai.

    ``close()`` (drena a cauda) / ``cancel()`` (aborta, descarta) fecham a captura e devolvem o
    ``capture_closed`` (texto + métricas). Fail-loud: conexão morta ⇒ ``capture_error`` tipado ao
    consumidor + desbloqueio de qualquer espera pendente. A instância É o read-loop; o consumidor
    só liga o ``on_event`` e chama ``close``/``cancel``."""

    def __init__(self, fh, session: str, on_event, *, open_header: dict,
                 open_timeout: float = 8.0):
        self._fh = fh
        self._session = session
        self._on_event = on_event
        self._lock = threading.Lock()
        self._opened = threading.Event()
        self._closed = threading.Event()
        self._open_ack: "dict | None" = None
        self._result: "dict | None" = None
        self._end_sent = False
        self._reader = threading.Thread(target=self._read_loop, name="vox-capture-reader",
                                        daemon=True)
        self._reader.start()
        # dispara o capture_open DEPOIS de o leitor estar de pé (segmentos/levels podem chegar
        # ANTES do ack — o feeder do mic já roda; o leitor os entrega ao on_event, sem perder).
        try:
            self._send_frame(open_header)
        except Exception as exc:  # noqa: BLE001 — pipe já morto no envio do open: fail-loud tipado
            self._teardown()
            raise CaptureError("pipe_broken", f"falha ao enviar capture_open: {exc}") from exc
        if not self._opened.wait(open_timeout):
            self._teardown()
            raise CaptureError("capture_timeout",
                               f"capture_open sem ack em {open_timeout:.1f}s")
        ack = self._open_ack or {}
        if ack.get("event") != "capture_open" or not ack.get("ok", False):
            self._teardown()
            raise CaptureError(ack.get("code") or "capture_failed",
                               ack.get("message") or "capture_open falhou",
                               owner=ack.get("owner"))

    @property
    def session(self) -> str:
        return self._session

    @property
    def closed(self) -> bool:
        return self._closed.is_set()

    def close(self, timeout: float = 8.0) -> dict:
        """Fecha a captura (drena a cauda transcrita) e devolve o ``capture_closed``
        (``text``/``peak``/``duration_s``/``chunks``/``errors``/``mic_ok``…)."""
        return self._end("capture_close", timeout)

    def cancel(self, timeout: float = 8.0) -> dict:
        """Aborta a captura (DESCARTA a cauda sem transcrever) e devolve o ``capture_closed``
        (``cancelled=True``)."""
        return self._end("capture_cancel", timeout)

    def _end(self, cmd: str, timeout: float) -> dict:
        with self._lock:
            already = self._end_sent
            self._end_sent = True
        if not already:
            try:
                self._send_frame({"cmd": cmd, "session": self._session, "req_id": _cap_rid()})
            except Exception as exc:  # noqa: BLE001 — pipe morto: o leitor já sinaliza pipe_broken
                self._closed.wait(timeout)
                self._teardown()
                if self._result is not None:
                    return self._result
                raise CaptureError("pipe_broken", f"falha ao enviar {cmd}: {exc}") from exc
        if not self._closed.wait(timeout):
            self._teardown()
            raise CaptureError("capture_timeout",
                               f"{cmd} sem capture_closed em {timeout:.1f}s")
        self._teardown()
        return self._result or {"event": "capture_closed", "session": self._session, "ok": False}

    # ------------------------------------------------------------ read-loop
    def _read_loop(self) -> None:
        recv_exact = make_recv_exact(self._fh.read)
        while True:
            try:
                header, _ = read_message(recv_exact)
            except (EOFError, ProtocolError, OSError, ValueError):
                self._fail_closed_local("pipe_broken", "conexão de captura caiu")
                return
            ev = header.get("event")
            if ev == "capture_open":
                self._open_ack = header
                self._opened.set()
                continue
            if ev == "capture_closed":
                self._result = header
                self._opened.set()             # defensivo: destrava um open ainda pendente
                self._closed.set()
                return
            if ev == "error":
                # erro de CONTROLE (no_session/capture_unavailable/not_open/not_owner/…): termina.
                if not self._opened.is_set():
                    self._open_ack = header     # falhou no open → o ctor levanta CaptureError
                else:
                    self._result = header       # falhou no close → vira o resultado
                self._opened.set()
                self._closed.set()
                return
            self._emit(header)                  # stream: capture_segment / capture_level / erro

    def _emit(self, header: dict) -> None:
        try:
            self._on_event(header)
        except Exception:  # noqa: BLE001 — o callback do consumidor NUNCA derruba o leitor
            pass

    def _fail_closed_local(self, code: str, message: str) -> None:
        """A conexão morreu (ou nunca respondeu): fabrica um ``capture_error`` TIPADO local,
        entrega ao consumidor e desbloqueia open/close pendentes com um resultado fail-loud."""
        self._emit({"event": "capture_error", "session": self._session,
                    "code": code, "message": message})
        if self._open_ack is None:
            self._open_ack = {"event": "error", "session": self._session,
                              "code": code, "message": message}
        if self._result is None:
            self._result = {"event": "capture_closed", "session": self._session,
                            "ok": False, "code": code, "message": message}
        self._opened.set()
        self._closed.set()

    def _send_frame(self, header: dict) -> None:
        data = encode_message(header)
        total = len(data)
        sent = 0
        while sent < total:
            n = self._fh.write(data if sent == 0 else data[sent:])
            if n is None:                       # handle não conta bytes (mocks) → assume tudo
                break
            if n <= 0:
                raise OSError("write da captura não progrediu")
            sent += n
        self._fh.flush()

    def _teardown(self) -> None:
        try:
            self._fh.close()
        except Exception:  # noqa: BLE001
            pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        if not self._closed.is_set():
            try:
                self.close()
            except Exception:  # noqa: BLE001 — o exit nunca mascara a exceção original
                pass


class VoxClient:
    """Cliente **síncrono e serializado** do daemon ``vox-engine`` via named pipe.

    Abre o pipe como arquivo (``open(pipe, "r+b", buffering=0)`` — sem ``pywin32``),
    fala o framing ``struct.pack(">II", json_len, audio_len) + json + audio`` e
    serializa **1 request/resposta por vez** com um lock; a resposta é lida numa
    thread com timeout (um daemon travado no meio do frame vira erro, não congela).

    Diferença importante: este cliente **NÃO** é o
    :class:`vox_engine.client.pipe_client.VoxClient` concorrente (que roteia
    múltiplos pedidos em voo por ``req_id``). Aqui há no máximo um pedido pendente —
    adequado a um consumidor sequencial (ditado, TTS on-demand).
    """

    def __init__(self, pipe: str = DEFAULT_PIPE, connect_timeout: float = 5.0, *,
                 auto_reconnect: bool = False, reconnect_timeout: float = 0.25):
        self.pipe_name = pipe
        self._lock = threading.Lock()   # serializa 1 request/resposta
        self._fh = None
        self._rid = 0
        # auto_reconnect (opt-in, retrocompatível): quando o handle morre, o cliente o
        # REABRE sozinho — o consumidor não reimplementa a camada de reconexão. Disciplina
        # (idêntica à do ditado/voice-chat, validada): REPETE o request só na falha de
        # ESCRITA (o frame comprovadamente NÃO chegou -> idempotente-seguro); NUNCA repete
        # em leitura/timeout (o motor PODE ter processado -> evita double-process). O handle
        # morto é descartado e a PRÓXIMA chamada reconecta. reconnect_timeout é o "cushion"
        # (curto: absorve um ERROR_PIPE_BUSY transitório do pipe compartilhado sem o stall
        # do connect inicial).
        self._auto_reconnect = bool(auto_reconnect)
        self._reconnect_timeout = max(0.0, reconnect_timeout)
        deadline = time.time() + max(0.0, connect_timeout)
        last_err = None
        while True:
            try:
                self._fh = open(pipe, "r+b", buffering=0)  # noqa: SIM115
                break
            except OSError as e:
                last_err = e
                if time.time() >= deadline:
                    raise
                time.sleep(0.1)
        if self._fh is None:  # pragma: no cover - defensivo
            raise (last_err or OSError("não abriu o pipe"))

    @classmethod
    def try_connect(cls, pipe: str = DEFAULT_PIPE,
                    connect_timeout: float = 2.0, *,
                    auto_reconnect: bool = False,
                    reconnect_timeout: float = 0.25) -> "VoxClient | None":
        """Conecta se o daemon existir; senão devolve ``None`` (não levanta).

        É a política "reusa se existe": tenta o motor único e, se não estiver no
        ar, o chamador cai para outra estratégia. ``auto_reconnect`` propaga para o
        cliente (self-healing opt-in)."""
        try:
            return cls(pipe, connect_timeout, auto_reconnect=auto_reconnect,
                       reconnect_timeout=reconnect_timeout)
        except (OSError, ValueError):
            return None

    @property
    def connected(self) -> bool:
        return self._fh is not None

    # ---- I/O bruto ----
    def _close_fh(self):
        try:
            if self._fh:
                self._fh.close()
        except OSError:
            pass
        self._fh = None

    @staticmethod
    def _read_exact(fh, n: int) -> bytes:
        """Lê n bytes de UM handle específico (nunca relê self._fh — uma thread
        leitora abandonada fica presa no SEU pipe, não migra p/ um reconectado)."""
        buf = b""
        while len(buf) < n:
            chunk = fh.read(n - len(buf))
            if not chunk:
                raise EOFError("pipe fechado")
            buf += chunk
        return buf

    def _try_reopen(self) -> bool:
        """Reabre o handle do pipe com o cushion (``reconnect_timeout``). True se
        reconectou. Só age com ``auto_reconnect``. Best-effort, nunca levanta.

        DEVE ser chamado com ``self._lock`` já retido (é chamado de dentro do
        ``_request``, que serializa) — não readquire o lock."""
        if not self._auto_reconnect:
            return False
        self._close_fh()
        deadline = time.time() + self._reconnect_timeout
        while True:
            try:
                self._fh = open(self.pipe_name, "r+b", buffering=0)  # noqa: SIM115
                return True
            except OSError:
                if time.time() >= deadline:
                    self._fh = None
                    return False
                time.sleep(0.05)

    def _send_frame(self, fh, header: dict, audio: bytes) -> None:
        """Escreve UM frame COMPLETO. ``FileIO.write`` (buffering=0) pode fazer um
        *short write* e devolver menos bytes SEM levantar — então escrevemos em laço
        até esgotar. Distinção crítica p/ o auto_reconnect:
          - 0 byte enviado antes da falha  -> levanta normal (frame NÃO chegou =>
            o chamador pode reconectar+repetir com segurança);
          - >0 byte enviado antes da falha -> levanta :class:`_PartialSend` (o frame
            chegou EM PARTE => reenviar duplicaria/corromperia -> NÃO repetir)."""
        data = encode_message(header, audio)
        total = len(data)
        sent = 0
        try:
            while sent < total:
                # bytes (não memoryview): handles/mocks fazem .decode/slice sobre bytes.
                n = fh.write(data if sent == 0 else data[sent:])
                if n is None:          # handle não conta bytes (convenção/mocks) -> assume tudo
                    sent = total
                    break
                if n <= 0:             # sem progresso: não laça infinito
                    raise OSError("write não progrediu (motor não consumiu o frame)")
                sent += n
            fh.flush()
        except Exception as exc:  # noqa: BLE001
            if sent > 0:
                raise _PartialSend(sent) from exc
            raise

    def _read_reply(self, fh, timeout: float) -> "tuple[dict, bytes]":
        """Lê a resposta de ``fh`` com TETO de tamanho e TIMEOUT numa thread presa ao
        handle. Timeout -> solta o handle e levanta; erro de frame -> fecha e levanta.
        NUNCA é repetido pelo chamador (o motor pode já ter processado)."""
        out: dict = {}

        def _do_read():
            try:
                head = self._read_exact(fh, _HDR.size)
                jl, al = _HDR.unpack(head)
                if jl > MAX_JSON or al > MAX_AUDIO or jl == 0:
                    raise ProtocolError(f"frame absurdo (jl={jl}, al={al})")
                body = self._read_exact(fh, jl)
                audio_out = self._read_exact(fh, al) if al else b""
                out["resp"] = (json.loads(body.decode("utf-8")), audio_out)
            except Exception as exc:  # noqa: BLE001
                out["err"] = exc

        rt = threading.Thread(target=_do_read, daemon=True)
        rt.start()
        rt.join(timeout)
        if rt.is_alive():
            # Não fechamos com read() pendente (bloquearia no lock do objeto);
            # soltamos a referência viva — a thread morre quando o handle fechar.
            if self._fh is fh:
                self._fh = None
            raise VoxEngineError(
                f"timeout ({timeout:.0f}s) aguardando resposta do motor")
        if "err" in out:
            self._close_fh()
            raise VoxEngineError(f"falha de protocolo: {out['err']}")
        return out["resp"]

    def _request(self, header: dict, audio: bytes = b"",
                 timeout: float = 120.0) -> "tuple[dict, bytes]":
        """Envia 1 frame e lê a resposta com TETO de tamanho e TIMEOUT.

        Serializado pelo lock (single-in-flight). Com ``auto_reconnect``: um handle
        morto é REABERTO de forma preguiçosa antes do envio, e uma falha de ESCRITA
        dispara reconectar + REPETIR o envio UMA vez (o frame não chegou ao motor —
        idempotente-seguro). Falha de LEITURA/TIMEOUT NUNCA é repetida (o motor pode
        ter processado) — só solta o handle e levanta; a próxima chamada reconecta."""
        with self._lock:
            # (1) handle morto -> reconecta preguiçoso (auto_reconnect) antes de enviar.
            if self._fh is None and self._auto_reconnect:
                self._try_reopen()
            fh = self._fh
            if fh is None:
                raise VoxEngineError("conexão com o motor caiu")

            # (2) envio; falha de ESCRITA => frame não chegou => reconecta + repete 1x.
            #     envio PARCIAL (_PartialSend) => bytes já chegaram => NÃO repete.
            try:
                self._send_frame(fh, header, audio)
            except _PartialSend as exc:
                self._close_fh()
                raise VoxEngineError(
                    f"envio parcial ao motor ({exc.sent} bytes) — não repetível") from exc
            except Exception as exc:  # noqa: BLE001
                self._close_fh()
                if not (self._auto_reconnect and self._try_reopen()):
                    raise VoxEngineError(f"falha ao enviar ao motor: {exc}") from exc
                fh = self._fh
                try:
                    self._send_frame(fh, header, audio)
                except _PartialSend as exc2:
                    self._close_fh()
                    raise VoxEngineError(
                        f"envio parcial ao motor ({exc2.sent} bytes) — não repetível") from exc2
                except Exception as exc2:  # noqa: BLE001
                    self._close_fh()
                    raise VoxEngineError(f"falha ao enviar ao motor: {exc2}") from exc2

            # (3) leitura; o envio JÁ foi bem-sucedido -> NÃO repete (evita double-process).
            return self._read_reply(fh, timeout)

    # ---- comandos de alto nível ----
    def _next_rid(self) -> str:
        self._rid += 1
        return str(self._rid)

    def ping(self, timeout: float = 5.0) -> bool:
        """True se o daemon respondeu ``pong``."""
        h, _ = self._request({"cmd": "ping", "req_id": self._next_rid()}, timeout=timeout)
        return h.get("event") == "pong"

    def info(self, timeout: float = 5.0) -> dict:
        """Estado do motor: ``{version, model, provider, stt_ready, tts_ready, ...}``."""
        h, _ = self._request({"cmd": "info", "req_id": self._next_rid()}, timeout=timeout)
        return h

    def devices(self, timeout: float = 5.0) -> dict:
        """Microfones vistos pelo daemon — a MESMA enumeração/seleção do ditado (UMA fonte).

        Devolve ``{"input": [{"index", "name", "is_default"}], "default_input": <nome|None>}``;
        ``input`` já vem DEDUPLICADO por nome (uma entrada por mic físico, na melhor host API).
        Passe o ``name`` escolhido em ``capture_open(input_device=<name>)`` — PORTÁVEL (o índice
        do PortAudio muda ao plugar/desplugar USB; o nome é estável). Reusar isto deixa o
        consumidor SEM ``sounddevice`` local (casca fina). Fail-safe: sem PortAudio → ``input=[]``
        (só o 'Automático'). Só ENTRADA — a saída do TTS é playback do consumidor (não do daemon)."""
        h, _ = self._request({"cmd": "devices", "req_id": self._next_rid()}, timeout=timeout)
        return {"input": h.get("input") or [], "default_input": h.get("default_input")}

    def update_check(self, timeout: float = 8.0) -> dict:
        """Pergunta ao motor se há release ASSINADA mais nova (endpoint READ-ONLY
        ``update_check``) — o consumidor decide quando chamar :meth:`update`. Nunca levanta;
        daemon legado sem o comando → ``update_available=None``. Retorna
        ``{'current','latest','update_available'}``."""
        try:
            h, _ = self._request({"cmd": "update_check", "req_id": self._next_rid()},
                                 timeout=timeout)
        except Exception:  # noqa: BLE001
            return {"current": None, "latest": None, "update_available": None}
        if h.get("event") != "update_check":         # daemon legado (bad_cmd) → desconhecido
            return {"current": h.get("version"), "latest": None, "update_available": None}
        return {"current": h.get("current"), "latest": h.get("latest"),
                "update_available": h.get("update_available")}

    def update(self, *, force: bool = False, with_translation: bool = True,
               boot_timeout_ms: "int | None" = None) -> dict:
        """Pede a ATUALIZAÇÃO do motor via o comando COORDENADO ``update`` (contrato seguro:
        só recicla OCIOSO DE VERDADE, ENFILEIRA se ocupado → ``deferred``, e responde
        ``in_progress`` a um update concorrente). Retorna ``{action, from, to, ...}`` NA HORA
        (a troca acontece em background). Daemon LEGADO sem o coordenador (``bad_cmd``) → cai
        para o update orquestrado pelo cliente (:func:`update_engine`, só com o guard de sessão).
        ``force`` atualiza mesmo com trabalho em voo."""
        try:
            h, _ = self._request({"cmd": "update", "force": bool(force),
                                  "req_id": self._next_rid()}, timeout=15.0)
        except Exception:  # noqa: BLE001
            h = None
        if isinstance(h, dict) and h.get("event") == "update":
            return {k: v for k, v in h.items() if k not in ("event", "req_id")}
        try:
            from vox_lifecycle import update_engine
        except ImportError as _e:
            raise VoxEngineError(
                "VoxClient.update() requires vox_lifecycle.py vendored alongside vox_sdk.py") from _e
        return update_engine(self.pipe_name, force=force, with_translation=with_translation,
                             boot_timeout_ms=boot_timeout_ms)

    def update_status(self, timeout: float = 8.0) -> dict:
        """Estado do update no motor (``state``/``current``/``latest``/``update_available``/
        ``pending``), via o endpoint READ-ONLY ``update_status``. Daemon legado → ``state``
        ``unknown`` + o que o ``update_check`` souber. Nunca levanta."""
        try:
            h, _ = self._request({"cmd": "update_status", "req_id": self._next_rid()},
                                 timeout=timeout)
        except Exception:  # noqa: BLE001
            return {"state": "unknown"}
        if h.get("event") != "update_status":
            return {"state": "unknown", **self.update_check(timeout=timeout)}
        return {k: v for k, v in h.items() if k not in ("event", "req_id")}

    def capabilities(self, timeout: float = 5.0) -> Capabilities:
        """Retrato TIPADO das capacidades desta máquina (uma chamada → cliente sabe tudo).

        Faz o handshake ``info`` e devolve um :class:`Capabilities`: perfis já resolvidos
        pro hardware (``transcription``/``transcription_hq``→turbo; ``translator``→large-v3
        na GPU — único perfil com large), modelos residentes/permitidos, formatos de áudio
        servíveis + as listas estáticas conhecidas.
        É a forma nativa de descoberta — o cliente não hardcoda nem lê o código-fonte."""
        h = self.info(timeout=timeout)
        # Coerção defensiva de TIPO (não só falsy): um daemon degradado/hostil pode mandar
        # ``models`` como lista etc. — degradamos para vazio em vez de quebrar/devolver lixo.
        def _d(v):
            return v if isinstance(v, dict) else {}

        def _l(v):
            return v if isinstance(v, list) else []
        return Capabilities(
            version=h.get("version"),
            provider=h.get("provider"),
            hardware=h.get("hardware") if isinstance(h.get("hardware"), dict) else None,
            profiles=_d(h.get("profiles")),
            models_resident=sorted(_d(h.get("models")).keys()),
            models_allowed=_l(h.get("allowed_models")),
            audio_formats_available=_l(h.get("encode_formats")),
            voices=_l(h.get("tts_voices")),
            supported_voices=_l(h.get("supported_voices")),
            default_voice=h.get("default_voice"),
        )

    def transcribe(self, audio, lang: str = "", session: str = "default",
                   priority: str = "interactive", profile: "str | None" = None,
                   model: "str | None" = None, timeout: float = 120.0) -> str:
        """Transcreve ``audio`` (float32 16k mono: bytes, ndarray ou iterável) e
        devolve o texto. Levanta :class:`VoxEngineError` se o motor não retornar
        ``result``.

        ``profile`` ('dictation'|'translator') deixa o daemon escolher o modelo pelo
        hardware — ``translator`` usa large-v3 na GPU (turbo no CPU) para tradução de
        qualidade; ``dictation`` (default do daemon) usa turbo. ``model`` força um
        modelo específico (ex.: 'large-v3'), tendo precedência sobre o profile."""
        pcm = _to_pcm_bytes(audio)
        header: dict = {"cmd": "transcribe", "req_id": self._next_rid(),
                        "session": session, "lang": lang or "",
                        "priority": priority}
        if profile is not None:
            header["profile"] = profile
        if model is not None:
            header["model"] = model
        h, _ = self._request(header, pcm, timeout=timeout)
        if h.get("event") == "result":
            return (h.get("text") or "").strip()
        raise VoxEngineError(
            f"motor retornou {h.get('event')}/{h.get('code')}: {h.get('message') or ''}")

    def transcribe_file(self, audio, lang: str = "", session: str = "default",
                        priority: str = "interactive", profile: "str | None" = None,
                        model: "str | None" = None, timeout: float = 300.0) -> str:
        """Transcreve um áudio COMPLETO (gravação inteira) e devolve o texto TODO.

        Diferente de :meth:`transcribe` (que assume um trecho curto <30s, o caso do
        ditado streaming), esta trilha manda o áudio inteiro e o motor SEGMENTA no
        servidor (VAD nos vales de silêncio / janela fixa), transcrevendo tudo sem o teto
        de ~30s do Whisper. É o caso "gateway com o áudio pronto" (batch/arquivo).

        ``timeout`` é maior (batch). Sem ``model``/``profile``, usa o profile
        ``transcription`` = **turbo** (rápido, mesmo motor do ditado, zero VRAM nova).
        Por política do motor, o ``large-v3`` é reservado à tradução em tempo real:
        arquivo (inclusive ``profile='transcription_hq'``) NUNCA sobe large — fica em turbo.
        Levanta :class:`VoxEngineError` se o motor não retornar ``result``."""
        pcm = _to_pcm_bytes(audio)
        header: dict = {"cmd": "transcribe_file", "req_id": self._next_rid(),
                        "session": session, "lang": lang or "",
                        "priority": priority}
        if profile is not None:
            header["profile"] = profile
        if model is not None:
            header["model"] = model
        h, _ = self._request(header, pcm, timeout=timeout)
        if h.get("event") == "result":
            return (h.get("text") or "").strip()
        raise VoxEngineError(
            f"motor retornou {h.get('event')}/{h.get('code')}: {h.get('message') or ''}")

    def tts(self, text: str, fmt: str = "pcm", voice: "str | None" = None,
            speed: float = 1.0, session: str = "default",
            priority: str = "interactive", normalize: bool = False,
            timeout: float = 120.0):
        """Sintetiza ``text``.

        Com ``fmt="pcm"`` (default) devolve ``(header, samples)`` — ``samples`` é um
        ``numpy.ndarray`` float32 quando numpy existe, senão os ``bytes`` PCM crus
        (numpy é opcional no consumidor). Com um formato comprimido
        (``opus``/``mp3``/``wav``/``vorbis``) devolve ``(header, bytes)`` já
        codificados. O tipo do retorno depende SÓ do ``fmt`` deste request.

        ``normalize=True`` pede o peak-normalize na FONTE (pico->0.92 antes do
        PCM/encode) — nível consistente entre chamadas sem renormalizar no cliente;
        default OFF = PCM cru byte-idêntico. Fade NÃO é da fonte (é playback)."""
        req = {"cmd": "tts", "req_id": self._next_rid(), "session": session,
               "text": text, "voice": voice, "speed": speed, "priority": priority}
        if normalize:
            req["normalize"] = True
        if fmt and fmt != "pcm":
            req["format"] = fmt
        h, audio = self._request(req, timeout=timeout)
        if fmt and fmt != "pcm":
            return h, audio                      # bytes codificados, sem interpretar
        return _pcm_reply(h, audio)              # PCM→ndarray (numpy lazy) ou bytes crus

    def capture_open(self, session: str, on_event, *, lang: str = "",
                     model: "str | None" = None, profile: "str | None" = None,
                     input_device: "str | int | None" = None, min_rms: float = 0.0,
                     connect_timeout: float = 5.0, open_timeout: float = 8.0) -> "CaptureHandle":
        """Abre uma captura de sessão (mic → transcrição no daemon) e devolve um
        :class:`CaptureHandle`. ``on_event(frame)`` recebe CADA evento assíncrono —
        ``capture_segment`` {idx,text} / ``capture_level`` {rms,peak,silent} / ``capture_error``
        {code,message} — discrimine por ``frame['event']``. ``handle.close()`` (drena) /
        ``handle.cancel()`` (aborta) devolvem o ``capture_closed`` com o resultado.

        Levanta :class:`CaptureError` TIPADO se o mic estiver ocupado/indisponível ou o ack não
        vier (``busy``/``mic_busy``/``mic_open_failed``/``capture_unavailable``/``capture_timeout``…).
        Fase 0 = single-owner: uma 2ª captura concorrente responde ``busy``.

        Usa uma conexão de pipe DEDICADA (a captura é stream, não request/resposta) — NÃO
        compartilha o handle serializado deste cliente, então TTS/transcribe seguem em paralelo.

        CONTRATO DE PROPRIEDADE DO MIC (importante): o DAEMON é o dono do microfone físico. Tu
        PEDES a captura com ``capture_open``; QUEM abre e arbitra o device é o daemon — tu não
        controlas a conexão do mic. Portanto o consumidor NÃO deve adquirir/escrever nenhum
        ``mic.lock`` próprio: o daemon REIVINDICA um lock de qualquer consumidor (o teu sid OU o
        de outro fork/sessão) e só CEDE a um dono físico real (o ditado). A serialização entre teus
        forks/sessões é do daemon (2ª captura → ``busy``). Regra: PEÇA a captura e trate
        ``mic_busy`` — não trave o mic tu mesmo. Quando ``mic_busy`` vier (só do ditado ativo),
        ``CaptureError.owner`` traz o sid do dono (ex.: ``vox-dictate``) para um rótulo preciso."""
        if not session:
            raise ValueError("capture_open requer session")
        header: dict = {"cmd": "capture_open", "req_id": _cap_rid(), "session": session}
        if lang:
            header["lang"] = lang
        if model:
            header["model"] = model
        if profile:
            header["profile"] = profile
        if input_device is not None:
            header["input_device"] = input_device
        if min_rms:
            header["min_rms"] = float(min_rms)
        fh = self._open_capture_pipe(connect_timeout)
        return CaptureHandle(fh, session, on_event, open_header=header, open_timeout=open_timeout)

    def _open_capture_pipe(self, connect_timeout: float):
        """Conexão de pipe NOVA e OVERLAPPED (ctypes) para a captura (stream): a thread-leitora
        pode ficar bloqueada num ``ReadFile`` enquanto o consumidor escreve o ``close`` no MESMO
        handle — o que um handle SÍNCRONO serializaria (deadlock). O handle síncrono
        (``open(pipe,"r+b")``) só é seguro no ``VoxClient`` serial de request/resposta."""
        return _OverlappedPipe(self.pipe_name, connect_timeout)

    def encode_formats(self, timeout: float = 5.0) -> "list[str]":
        """Formatos de saída de TTS servíveis pelo daemon (capability discovery)."""
        return list(self.info(timeout=timeout).get("encode_formats") or ["pcm"])

    def translate(self, audio, from_lang: "str | None" = None, to_lang: str = "pt",
                  session: str = "default", whisper_model: "str | None" = None,
                  priority: str = "interactive", speak: bool = False,
                  dub_voice: "str | None" = None, dub_sid: int = 0, dub_fmt: str = "pcm",
                  timeout: float = 120.0):
        """Traduz ``audio`` (float32 16k mono) -> texto no idioma ``to_lang`` e, com
        ``speak=True``, também a VOZ dublada no idioma-alvo (Fase B).

        Retorno (depende SÓ de ``speak``/``dub_fmt`` deste request):

        - ``speak=False`` -> ``dict`` (header do resultado: ``text``/``source_text``/
          ``src_lang``/``tgt_lang``/…) — compat total com a Fase A.
        - ``speak=True, dub_fmt="pcm"`` -> ``(header, samples)``: ``samples`` é um
          ``numpy.ndarray`` float32 quando numpy existe (senão os ``bytes`` PCM crus);
          ``ndarray`` vazio se o daemon marcou ``dub_skipped``.
        - ``speak=True, dub_fmt!="pcm"`` -> ``(header, bytes)`` já codificados.

        A voz de dublagem é escolhida pelo idioma-ALVO no daemon (NÃO é a voz do
        ``cmd tts``); ``dub_voice`` só força uma alternativa compatível com o alvo."""
        pcm = _to_pcm_bytes(audio)
        header: dict = {"cmd": "translate", "req_id": self._next_rid(),
                        "session": session, "to_lang": to_lang, "priority": priority}
        if from_lang is not None:                 # presente (mesmo "") = explícito
            header["from_lang"] = from_lang
        if whisper_model is not None:
            header["whisper_model"] = whisper_model
        if speak:
            header["speak"] = True
            if dub_voice:
                header["dub_voice"] = dub_voice
            if dub_sid:
                header["dub_sid"] = dub_sid
            if dub_fmt and dub_fmt != "pcm":
                header["dub_fmt"] = dub_fmt
        h, audio_out = self._request(header, pcm, timeout=timeout)
        if not speak:
            return h                              # compat: só o header (dict)
        if dub_fmt and dub_fmt != "pcm":
            return h, audio_out                   # bytes codificados, sem interpretar
        return _pcm_reply(h, audio_out)           # PCM→ndarray (numpy lazy) ou bytes crus

    def translate_text(self, text: str, from_lang: str, to_lang: str = "pt", *,
                       session: str = "default", priority: str = "interactive",
                       speak: bool = False, dub_voice: "str | None" = None,
                       dub_sid: int = 0, dub_fmt: str = "pcm", timeout: float = 120.0):
        """Traduz TEXTO -> texto no idioma ``to_lang`` (pula o STT: o chamador JÁ
        transcreveu a fala) e, com ``speak=True``, também a VOZ dublada (Fase B).

        ``from_lang`` é OBRIGATÓRIO — o Argos NÃO auto-detecta idioma de TEXTO. Retorno
        idêntico ao :meth:`translate` (depende SÓ de ``speak``/``dub_fmt``):

        - ``speak=False`` -> ``dict`` (só o header do resultado);
        - ``speak=True, dub_fmt="pcm"`` -> ``(header, samples)`` (``numpy.ndarray``
          float32 quando numpy existe, senão ``bytes`` PCM crus; vazio se ``dub_skipped``);
        - ``speak=True, dub_fmt!="pcm"`` -> ``(header, bytes)`` já codificados."""
        header: dict = {"cmd": "translate_text", "req_id": self._next_rid(),
                        "session": session, "text": text, "from_lang": from_lang,
                        "to_lang": to_lang, "priority": priority}
        if speak:
            header["speak"] = True
            if dub_voice:
                header["dub_voice"] = dub_voice
            if dub_sid:
                header["dub_sid"] = dub_sid
            if dub_fmt and dub_fmt != "pcm":
                header["dub_fmt"] = dub_fmt
        h, audio_out = self._request(header, timeout=timeout)   # sem payload de áudio
        if not speak:
            return h                              # compat: só o header (dict)
        if dub_fmt and dub_fmt != "pcm":
            return h, audio_out                   # bytes codificados, sem interpretar
        return _pcm_reply(h, audio_out)           # PCM→ndarray (numpy lazy) ou bytes crus

    def prepare_translation(self, from_lang: str, to_lang: str,
                            whisper_model: "str | None" = None, speak: bool = False,
                            dub_voice: "str | None" = None, dub_sid: int = 0,
                            timeout: float = 600.0) -> dict:
        """Baixa/instala o modelo faster-whisper + par(es) Argos ANTES de traduzir (o
        caminho de inferência nunca baixa). Com ``speak=True`` também faz o warm-up da
        voz de dublagem do idioma-alvo, FORA do worker — a resposta ``ready`` ganha
        ``dub_voice/dub_sample_rate/dub_provider/dub_ready``. Bloqueia até ``ready``
        (ou erro); ``timeout`` folgado (pode baixar centenas de MB na 1ª vez)."""
        header: dict = {"cmd": "prepare_translation", "req_id": self._next_rid(),
                        "from_lang": from_lang, "to_lang": to_lang}
        if whisper_model is not None:
            header["whisper_model"] = whisper_model
        if speak:
            header["speak"] = True
            if dub_voice:
                header["dub_voice"] = dub_voice
            if dub_sid:
                header["dub_sid"] = dub_sid
        h, _ = self._request(header, timeout=timeout)
        return h

    def close(self) -> None:
        with self._lock:
            self._close_fh()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
