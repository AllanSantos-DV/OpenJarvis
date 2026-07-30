"""vox-lifecycle (Python) — add-on OPCIONAL de ciclo de vida do motor ``vox-engine``.

Concentra TODO o maquinário de INSTALAÇÃO / ATUALIZAÇÃO / GESTÃO DO DAEMON: baixar +
VERIFICAR (Ed25519, fail-closed) a release assinada, rodar o ``install.ps1``, o lock
cross-process de instalação, subir/derrubar o daemon instalado e a máquina de estados
``ensure_vox`` / ``update_engine``. É **complementar** ao ``vox_sdk.py``:

* Um cliente REMOTO que só CONVERSA com um daemon já no ar vendoriza **apenas**
  ``vox_sdk.py`` — nada de instalador/updater.
* Um cliente que AUTO-INSTALA / AUTO-ATUALIZA o motor vendoriza ``vox_sdk.py`` +
  este ``vox_lifecycle.py`` + o verificador Ed25519 ``_ed25519_ref.py`` (ao lado).

Direção de dependência (INVARIANTE): ``vox_lifecycle`` importa de ``vox_sdk``
(``VoxClient`` / ``DEFAULT_PIPE`` / ``SDK_VERSION``) — NUNCA o contrário. O core
(``vox_sdk``) segue stdlib-puro e **não** puxa este módulo no import; o
``VoxClient.update()`` importa ``update_engine`` daqui de forma PREGUIÇOSA (e falha
alto se este arquivo não foi vendorizado junto).

Postura de segurança — **fail-closed**: o ``install.ps1`` da release só roda após uma
verificação Ed25519 (hash-then-sign sobre o SHA-256 do zip) bem-sucedida contra o
KEYRING ``PUBKEYS``. Chave malformada/ausente, ``.sig`` ausente, erro de rede ou
verificador indisponível ⇒ verificação falsa ⇒ o instalador NUNCA roda.

Fonte canônica: ``vox-engine/sdk/python/vox_lifecycle.py``. Clientes VENDORIZAM uma
cópia byte-idêntica (mesma convenção de lock/config do SDK Node irmão).
"""
from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import os
import shutil
import subprocess
import tempfile
import time
import types
import urllib.request
import zipfile

from vox_sdk import DEFAULT_PIPE, SDK_VERSION, VoxClient

# Windows: spawna filhos SEM alocar console — mata o flash de janela de
# ``powershell``/``taskkill``/``python.exe`` na init e no update. 0 fora do Windows.
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _try_show_splash(subtitle: str):
    """Mostra a splash do install/update (best-effort, NUNCA levanta). Desligável por
    ``VOX_SPLASH=0``. Import LAZY de ``vox_splash`` (flat vendorável) — ausência (consumidor
    que ainda não vendorou o arquivo) ⇒ no-op silencioso, retrocompatível."""
    try:
        import vox_splash
        return vox_splash.show_splash_async(subtitle=subtitle)
    except Exception:  # noqa: BLE001 — splash é cosmética; jamais bloqueia o install
        return None


def _close_splash(handle) -> None:
    if handle is None:
        return
    try:
        handle.close()
    except Exception:  # noqa: BLE001
        pass


# ---------------------------------------------------------------------------
# CONFIG canônica de release/instalação (os MESMOS valores no SDK Node irmão).
# ``DEFAULT_PIPE`` é IMPORTADO do vox_sdk (core) — não é redefinido aqui.
# ---------------------------------------------------------------------------
RELEASES_API = ("https://api.github.com/repos/AllanSantos-DV/"
                "engine-registry/releases")
TAG_PREFIX = "vox-engine-v"
INSTALLER_ASSET = "vox-engine-installer.zip"
SIG_ASSET = INSTALLER_ASSET + ".sig"

# KEYRING Ed25519 (hex, 64 chars cada) — rotação = [atual, próxima]. A verificação
# passa se QUALQUER chave validar. Vazio/algum item inválido (≠ 64 hex) ⇒ FATAL.
PUBKEYS = ["293263e73c4ba424a9ef3432d1ce55740fc0a68478f20235ca109c074ec83f52"]


def _install_root() -> str:
    """``%LOCALAPPDATA%\\vox-engine`` (fallback ``%USERPROFILE%``)."""
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return os.path.join(base, "vox-engine")


INSTALL_ROOT = _install_root()
INSTALLED_PYTHON = os.path.join(INSTALL_ROOT, "venv", "Scripts", "python.exe")
INSTALLED_PYTHONW = os.path.join(INSTALL_ROOT, "venv", "Scripts", "pythonw.exe")
INSTALLED_DICTATE = os.path.join(INSTALL_ROOT, "venv", "Scripts", "vox-dictate.exe")
DAEMON_LOG = os.path.join(INSTALL_ROOT, "logs", "daemon.log")
DAEMON_BOOT_LOG = os.path.join(INSTALL_ROOT, "logs", "daemon-boot.log")

# LOCK cross-process (IDÊNTICO ao Node — os dois coordenam entre si).
LOCK_DIR = os.path.join(INSTALL_ROOT, ".install.lock")
STALE_MS = 40 * 60 * 1000          # 40 min: > INSTALL_TIMEOUT_MS (30 min), senão
                                   # uma install legítima teria o lock roubado
ACQUIRE_TIMEOUT_MS = 180000        # 3 min esperando outro instalador
POLL_MS = 500                      # intervalo de poll da aquisição

# Timeouts da máquina de estados / instalação.
CONNECT_TIMEOUT_MS = 2000          # sonda de "pipe no ar?"
BOOT_TIMEOUT_MS = 150000           # 1ª carga GPU compila kernels (~1 min)
INSTALL_TIMEOUT_MS = 1800000       # 30 min: 1ª install baixa deps (wheels CUDA)


__all__ = [
    "PUBKEYS", "RELEASES_API", "TAG_PREFIX", "INSTALLER_ASSET", "SIG_ASSET",
    "INSTALL_ROOT", "INSTALLED_PYTHON", "INSTALLED_PYTHONW", "INSTALLED_DICTATE",
    "DAEMON_LOG", "DAEMON_BOOT_LOG", "LOCK_DIR",
    "verify_installer", "parse_version", "is_newer", "latest_release", "installed_version",
    "download_and_run_installer", "check_and_update",
    "start_installed_daemon", "stop_daemon",
    "ensure_vox", "ensure_vox_detailed", "update_engine",
]


# ---------------------------------------------------------------------------
# Verificador Ed25519 — REUSO do módulo já provado (`_ed25519_ref.verify`).
# NÃO reescrevemos crypto: carregamos a impl vendorizada (RFC 8032 Ap. A).
# ---------------------------------------------------------------------------
def _load_ed25519_verify():
    """Carrega ``_ed25519_ref.verify`` de forma robusta (drop-in): import direto,
    import relativo, ou carga por caminho ao lado deste arquivo. Se nada funcionar,
    devolve ``None`` (fail-closed no chamador)."""
    try:
        from _ed25519_ref import verify as _v  # type: ignore
        return _v
    except Exception:  # noqa: BLE001
        pass
    try:
        from ._ed25519_ref import verify as _v  # type: ignore
        return _v
    except Exception:  # noqa: BLE001
        pass
    try:
        here = os.path.dirname(os.path.abspath(__file__))
        path = os.path.join(here, "_ed25519_ref.py")
        spec = importlib.util.spec_from_file_location("_vox_ed25519_ref", path)
        if spec is None or spec.loader is None:
            return None
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod.verify
    except Exception:  # noqa: BLE001
        return None


_ed_verify = _load_ed25519_verify()


def verify_installer(blob: bytes, signature: bytes) -> bool:
    """Verifica a assinatura Ed25519 do instalador contra o KEYRING (``PUBKEYS``).

    Mensagem assinada = ``sha256(blob)`` (hash-then-sign, igual ao tools/sign_release.py).
    Para CADA chave do keyring chama ``_ed25519_ref.verify(pubkey, digest, sig)`` e
    devolve ``True`` se QUALQUER uma validar.

    **Fail-closed** (nunca levanta): devolve ``False`` se o keyring está vazio, se
    QUALQUER chave é malformada (≠ 64 hex / ≠ 32 bytes), se ``.sig`` está ausente ou
    não tem 64 bytes, se o verificador não pôde ser carregado, ou em qualquer erro.
    """
    try:
        keys = list(PUBKEYS)  # lê o global em tempo de chamada (rotação/testes)
    except Exception:  # noqa: BLE001
        return False
    if not keys:
        return False  # keyring vazio ⇒ FATAL
    raw_keys = []
    for k in keys:
        if not isinstance(k, str):
            return False
        ks = k.strip()
        if len(ks) != 64:
            return False  # chave malformada ⇒ FATAL (não instala)
        try:
            rk = bytes.fromhex(ks)
        except ValueError:
            return False
        if len(rk) != 32:
            return False
        raw_keys.append(rk)
    if _ed_verify is None:
        return False  # verificador indisponível ⇒ fail-closed
    if signature is None or not isinstance(signature, (bytes, bytearray)):
        return False  # .sig ausente ⇒ recusa
    sig = bytes(signature)
    if len(sig) != 64:
        return False
    if not isinstance(blob, (bytes, bytearray)):
        return False
    try:
        digest = hashlib.sha256(bytes(blob)).digest()
    except Exception:  # noqa: BLE001
        return False
    for rk in raw_keys:
        try:
            if _ed_verify(rk, digest, sig):
                return True
        except Exception:  # noqa: BLE001
            return False  # verificador levantou ⇒ fail-closed
    return False


def parse_version(v: str) -> "tuple[int, ...]":
    """'0.1.0' | 'vox-engine-v0.1.0' | 'v0.1.0' -> (0,1,0). Robusto a sufixos.

    FIX 7: só dígitos ASCII ``0-9`` contam (paridade EXATA com o Node e contrato
    never-throws). ``ch.isdigit()`` aceitaria '²' (isdigit()=True, mas int('²')
    LEVANTA ValueError) e dígitos Unicode ('١','๑','１') que ``int()`` converteria
    de forma divergente do Node — aqui qualquer não-ASCII encerra a parte."""
    v = (v or "").strip()
    if v.startswith(TAG_PREFIX):
        v = v[len(TAG_PREFIX):]
    v = v.lstrip("vV")
    nums = []
    for part in v.split("."):
        digits = ""
        for ch in part:
            if ch in "0123456789":     # SOMENTE ASCII 0-9 (nunca int() de Unicode)
                digits += ch
            else:
                break
        nums.append(int(digits) if digits else 0)
    return tuple(nums) or (0,)


def is_newer(candidate: str, current: str) -> bool:
    """True se ``candidate`` é estritamente maior que ``current``."""
    return parse_version(candidate) > parse_version(current)


# ---------------------------------------------------------------------------
# HTTP (urllib) + TLS corporativo opcional (truststore, import-guarded).
# ---------------------------------------------------------------------------
_TRUSTSTORE_TRIED = False


def _maybe_inject_truststore() -> None:
    """Best-effort: usa o cofre de certificados do SO (proxies de inspeção
    corporativa) SE ``truststore`` estiver instalado. Zero dependência obrigatória."""
    global _TRUSTSTORE_TRIED
    if _TRUSTSTORE_TRIED:
        return
    _TRUSTSTORE_TRIED = True
    try:
        import truststore  # type: ignore
        truststore.inject_into_ssl()
    except Exception:  # noqa: BLE001
        pass


def _default_http_get(url: str) -> bytes:
    req = urllib.request.Request(url, headers={
        "User-Agent": f"vox-sdk/{SDK_VERSION}",
        "Accept": "application/vnd.github+json",
    })
    with urllib.request.urlopen(req, timeout=30) as r:  # noqa: S310
        return r.read()


def latest_release(http_get=None) -> "dict | None":
    """Release mais nova do motor na vitrine que tenha AMBOS os assets
    (``vox-engine-installer.zip`` **e** ``.sig``): ``{version,tag,asset_url,sig_url}``
    ou ``None``. Sem ``.sig`` a release é inútil (fail-closed) → ignorada."""
    getter = http_get or _default_http_get
    if getter is _default_http_get:
        _maybe_inject_truststore()
    try:
        raw = getter(RELEASES_API)
        data = json.loads(raw.decode("utf-8") if isinstance(raw, (bytes, bytearray)) else raw)
    except Exception:  # noqa: BLE001
        return None
    best = None
    for rel in data if isinstance(data, list) else []:
        tag = rel.get("tag_name", "") or ""
        if not tag.startswith(TAG_PREFIX):
            continue
        asset_url = sig_url = None
        for a in rel.get("assets", []) or []:
            name = a.get("name")
            if name == INSTALLER_ASSET:
                asset_url = a.get("browser_download_url")
            elif name == SIG_ASSET:
                sig_url = a.get("browser_download_url")
        if not asset_url or not sig_url:
            continue  # precisa dos DOIS (sem sig não há como verificar)
        cand = {"version": tag[len(TAG_PREFIX):], "tag": tag,
                "asset_url": asset_url, "sig_url": sig_url}
        if best is None or is_newer(cand["version"], best["version"]):
            best = cand
    return best


def installed_version(python_exe: "str | None" = None, run=None) -> "str | None":
    """Versão do motor INSTALADO — roda o python do venv instalado
    (``import vox_engine; __version__``). ``None`` se ausente / import falho /
    install quebrado."""
    python_exe = python_exe or INSTALLED_PYTHON
    runner = run or subprocess.run
    try:
        if not python_exe or not os.path.exists(python_exe):
            return None
    except Exception:  # noqa: BLE001
        return None
    try:
        out = runner([python_exe, "-c",
                      "import vox_engine,sys;sys.stdout.write(vox_engine.__version__)"],
                     capture_output=True, text=True, timeout=30,
                     creationflags=_NO_WINDOW)
        v = (getattr(out, "stdout", "") or "").strip()
        return v or None
    except Exception:  # noqa: BLE001
        return None


# ---------------------------------------------------------------------------
# Download + install (fail-closed): baixa, VERIFICA, extrai, roda install.ps1.
# ---------------------------------------------------------------------------
def _kill_tree(proc) -> None:
    """Mata o processo do install E TODA a árvore de netos (py/venv/pip) no
    Windows. ``taskkill /PID <pid> /T /F`` derruba a árvore a partir da raiz.
    Best-effort, nunca levanta."""
    pid = getattr(proc, "pid", None)
    if pid is not None:
        try:
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                           stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=30,
                           creationflags=_NO_WINDOW)
        except Exception:  # noqa: BLE001
            pass
    try:
        proc.kill()
    except Exception:  # noqa: BLE001
        pass
    try:
        proc.wait(timeout=10)
    except Exception:  # noqa: BLE001
        pass


def _run_installer(args, *, timeout: float, log_path: str):
    """Roda o ``install.ps1`` destacando stdout/err para um ARQUIVO (não pipes:
    o install.ps1 gera netos py/venv/pip que herdariam pipes e poderiam travar).
    Mata a ÁRVORE inteira no timeout. Devolve um objeto com ``.returncode``."""
    proc = None
    try:
        with open(log_path, "wb") as outf:
            proc = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=outf,
                                    stderr=subprocess.STDOUT, close_fds=True,
                                    creationflags=_NO_WINDOW)
            rc = proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_tree(proc)
        return types.SimpleNamespace(returncode=-1, timed_out=True)
    except Exception:  # noqa: BLE001
        if proc is not None:
            _kill_tree(proc)
        raise
    return types.SimpleNamespace(returncode=rc, timed_out=False)


def _zip_assert_safe(z: "zipfile.ZipFile", dest: str) -> None:
    """GUARD de Zip Slip (defense-in-depth, CWE-22) para a extração do instalador — self
    contido no SDK (vendorável, stdlib-pura). Levanta ``ValueError`` ANTES de escrever se
    QUALQUER membro tentar escapar ``dest``: componente ``..``/caminho absoluto, alvo
    resolvido fora do destino, ou SYMLINK (o ``zipfile`` do Python não protege symlink;
    detectado nos 16 bits altos do ``external_attr`` = modo Unix, máscara S_IFLNK 0o120000).
    O blob já é verificado por Ed25519 antes daqui — isto é cinto-e-suspensório."""
    dest_abs = os.path.realpath(dest)
    prefix = dest_abs + os.sep
    for info in z.infolist():
        name = info.filename
        if (info.external_attr >> 16) & 0o170000 == 0o120000:
            raise ValueError(f"symlink recusado no zip: {name!r}")
        if os.path.isabs(name) or name.startswith(("/", "\\")):
            raise ValueError(f"caminho absoluto recusado no zip: {name!r}")
        target = os.path.realpath(os.path.join(dest_abs, name))
        if target != dest_abs and not target.startswith(prefix):
            raise ValueError(f"caminho fora do destino recusado no zip: {name!r}")


def download_and_run_installer(asset_url: str, sig_url: "str | None" = None, *,
                               http_get=None, run=None, extra_args=None,
                               pipe: "str | None" = None,
                               target_version: "str | None" = None,
                               connect=None, python_exe: "str | None" = None,
                               lock_dir: "str | None" = None,
                               stale_ms: int = STALE_MS,
                               acquire_timeout_ms: int = ACQUIRE_TIMEOUT_MS,
                               poll_ms: int = POLL_MS) -> bool:
    """Baixa o installer.zip + ``.sig``, VERIFICA Ed25519 (fail-closed), extrai e
    roda ``install.ps1 -NoStart``. Devolve ``True`` só com ``rc == 0``.

    **LOCK (FIX 4 — paridade Node)**: adquire o ``_InstallLock`` compartilhado e
    envolve re-checagem + verify + install. Assim o ``check_and_update`` PÚBLICO (e
    qualquer chamador direto) fica serializado entre processos/linguagens — não só o
    ``ensure_vox_detailed``. Se o lock não vier (outro instalador ativo), devolve
    ``True`` se já estivermos na ``target_version`` (o outro terminou), senão ``False``.

    **Re-check sob o lock**: se ``pipe`` está no ar (outro subiu o daemon) ou já
    estamos na ``target_version``, PULA a instalação (devolve ``True``).

    **Segurança**: sem ``sig_url`` recusa ANTES de baixar o blob (FIX 5, fail-fast);
    sem ``.sig`` baixável ou assinatura inválida ⇒ ``False`` e o ``install.ps1`` NUNCA
    roda. A extração usa os BYTES VERIFICADOS em memória (``io.BytesIO``), sem re-ler
    um arquivo em disco (FIX 6 — evita TOCTOU entre verify e extract). Limpa o temp
    SEMPRE e libera o lock SEMPRE (finally)."""
    # FIX 5: sem sig_url a release é inútil ⇒ recusa ANTES de baixar o blob (fail-fast).
    if not sig_url:
        return False  # fail-closed: release sem .sig ⇒ nunca instala

    getter = http_get or _default_http_get
    runner = run or _run_installer
    if getter is _default_http_get:
        _maybe_inject_truststore()

    def _already_current() -> bool:
        """True se o motor instalado já está na ``target_version`` (ou mais novo)."""
        if target_version is None:
            return False
        cur = installed_version(python_exe, run=run)
        return cur is not None and not is_newer(target_version, cur)

    # FIX 4: LOCK cross-process em volta de re-check + verify + install.
    lock = _InstallLock(lock_dir, stale_ms=stale_ms,
                        acquire_timeout_ms=acquire_timeout_ms, poll_ms=poll_ms)
    if not lock.acquire():
        # Outro instalador segurou o lock o tempo todo — não falha às cegas: ele
        # pode ter terminado. Se já estamos na versão-alvo, trata como sucesso.
        return _already_current()

    tmp = None
    try:
        # RE-CHECK sob o lock (corrida: alguém instalou/subiu enquanto esperávamos).
        if pipe:
            connector = connect or VoxClient.try_connect
            try:
                c = connector(pipe, 0.5)
                if c is not None:
                    try:
                        c.close()
                    except Exception:  # noqa: BLE001
                        pass
                    return True         # daemon subiu no meio-tempo
            except Exception:  # noqa: BLE001
                pass
        if _already_current():
            return True                 # outro instalador já satisfez o alvo

        try:
            blob = getter(asset_url)
        except Exception:  # noqa: BLE001
            return False
        try:
            signature = getter(sig_url)
        except Exception:  # noqa: BLE001
            return False
        if not verify_installer(blob, signature):
            return False  # assinatura inválida/ausente ⇒ NÃO executa (fail-closed)

        # FIX 6: extrai dos BYTES VERIFICADOS (io.BytesIO) — sem gravar/re-ler o zip
        # em disco entre o verify e a extração (fecha a janela TOCTOU).
        tmp = tempfile.mkdtemp(prefix="vox-sdk-install-")
        out_path = os.path.join(tmp, "install-output.log")
        try:
            with zipfile.ZipFile(io.BytesIO(bytes(blob))) as z:
                _zip_assert_safe(z, tmp)   # GUARD Zip Slip (fail-loud) antes de escrever
                z.extractall(tmp)
        except Exception:  # noqa: BLE001 — zip corrompido / download parcial / traversal
            return False
        install_ps1 = os.path.join(tmp, "install.ps1")
        if not os.path.exists(install_ps1):
            return False  # zip sem install.ps1 na raiz
        args = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                "-File", install_ps1, "-NoStart", *(extra_args or [])]
        splash = _try_show_splash("atualizando o motor de voz…")
        try:
            res = runner(args, timeout=INSTALL_TIMEOUT_MS / 1000.0, log_path=out_path)
        except Exception:  # noqa: BLE001 — powershell ausente etc.
            return False
        finally:
            _close_splash(splash)
        return getattr(res, "returncode", 1) == 0
    finally:
        lock.release()
        if tmp is not None:
            shutil.rmtree(tmp, ignore_errors=True)


def check_and_update(python_exe: "str | None" = None, *, http_get=None, run=None,
                     latest: "dict | None" = None, pipe: "str | None" = None,
                     connect=None, lock_dir: "str | None" = None,
                     with_translation: bool = True) -> dict:
    """Decide e executa a atualização. Devolve
    ``{'action','installed','latest'}`` com ``action`` ∈
    ``up_to_date|installed|updated|offline_installed|unavailable|failed``.

    Regras (fail-closed): sem release e sem instalado ⇒ ``unavailable``; sem
    release mas instalado ⇒ ``offline_installed`` (usa o existente); atual ⇒
    ``up_to_date`` (NÃO baixa); ausente/velho ⇒ baixa+verifica+instala; verify/
    download falho ⇒ NÃO roda install.ps1 (usa o existente se houver, senão
    ``failed``); pós-install não importável ⇒ ``failed`` (não sobe lixo).

    ``with_translation`` (padrão True): quando o install.ps1 REALMENTE roda, passa
    ``-WithTranslation`` — o consumidor já ganha o extra de tradução (faster-whisper
    + Argos) por padrão. Libs seguem LAZY em runtime; só a POLÍTICA de instalação
    muda. Passe False para o motor base apenas.

    O LOCK de instalação vive DENTRO de ``download_and_run_installer`` (FIX 4), então
    este ponto de entrada público também fica serializado entre processos."""
    python_exe = python_exe or INSTALLED_PYTHON
    if latest is None:
        latest = latest_release(http_get=http_get)
    cur = installed_version(python_exe, run=run)
    if latest is None:
        return {"action": ("offline_installed" if cur else "unavailable"),
                "installed": cur, "latest": None}
    if cur is not None and not is_newer(latest["version"], cur):
        return {"action": "up_to_date", "installed": cur, "latest": latest["version"]}
    ok = download_and_run_installer(latest["asset_url"], latest.get("sig_url"),
                                    http_get=http_get, run=run, pipe=pipe,
                                    target_version=latest["version"], connect=connect,
                                    python_exe=python_exe, lock_dir=lock_dir,
                                    extra_args=(["-WithTranslation"] if with_translation else None))
    if not ok:
        # update é best-effort: sem verify/download/install, usa o existente.
        return {"action": ("offline_installed" if cur else "failed"),
                "installed": cur, "latest": latest["version"]}
    new = installed_version(python_exe, run=run)
    if new is None:
        return {"action": "failed", "installed": cur, "latest": latest["version"]}
    return {"action": ("installed" if cur is None else "updated"),
            "installed": new, "latest": latest["version"]}


# ---------------------------------------------------------------------------
# LOCK cross-process (dir-lock atômico) — convenção IDÊNTICA ao SDK Node.
# ---------------------------------------------------------------------------
def _now_ms() -> int:
    return int(time.time() * 1000)


class _InstallLock:
    """Lock de instalação/atualização compartilhado entre processos (e linguagens).

    Aquisição por ``os.mkdir`` atômico em ``LOCK_DIR`` (falha se já existe). Grava
    ``meta.json`` = ``{"pid","ts","lang":"python"}`` dentro. Recupera locks órfãos
    (dir/meta mais velho que ``STALE_MS``). Poll a cada ``POLL_MS`` até
    ``ACQUIRE_TIMEOUT_MS``. Sempre libera em ``release`` (remove meta + rmdir)."""

    def __init__(self, lock_dir: "str | None" = None, *,
                 stale_ms: int = STALE_MS, acquire_timeout_ms: int = ACQUIRE_TIMEOUT_MS,
                 poll_ms: int = POLL_MS):
        self.lock_dir = lock_dir or LOCK_DIR   # lê o global em tempo de chamada
        self.meta_path = os.path.join(self.lock_dir, "meta.json")
        self.stale_ms = stale_ms
        self.acquire_timeout_ms = acquire_timeout_ms
        self.poll_ms = poll_ms
        self.acquired = False

    def _write_meta(self) -> None:
        try:
            with open(self.meta_path, "w", encoding="utf-8") as f:
                json.dump({"pid": os.getpid(), "ts": _now_ms(), "lang": "python"}, f)
        except Exception:  # noqa: BLE001 — meta é diagnóstico; não falha o lock
            pass

    def _lock_age_ms(self) -> "int | None":
        """Idade (ms) do lock: ``ts`` do meta.json, senão mtime do dir. None se sumiu."""
        try:
            with open(self.meta_path, "r", encoding="utf-8") as f:
                ts = json.load(f).get("ts")
            if isinstance(ts, (int, float)):
                return _now_ms() - int(ts)
        except Exception:  # noqa: BLE001 — sem meta legível: cai p/ mtime do dir
            pass
        try:
            return _now_ms() - int(os.path.getmtime(self.lock_dir) * 1000)
        except OSError:
            return None

    def _is_stale(self) -> bool:
        age = self._lock_age_ms()
        return age is not None and age > self.stale_ms

    def _steal(self) -> None:
        shutil.rmtree(self.lock_dir, ignore_errors=True)

    def _owned_by_us(self) -> bool:
        """True se o ``meta.json`` em disco tem o NOSSO pid (o lock ainda é nosso).

        Sem meta legível ou com pid diferente ⇒ ``False`` (nosso lock foi roubado por
        outro processo, que recriou o dir). Nunca levanta."""
        try:
            with open(self.meta_path, "r", encoding="utf-8") as f:
                return json.load(f).get("pid") == os.getpid()
        except Exception:  # noqa: BLE001 — sem prova de posse ⇒ não é (comprovadamente) nosso
            return False

    def acquire(self) -> bool:
        """Adquire o lock. ``True`` se conseguiu, ``False`` no timeout (outro
        instalador ativo). Cria o diretório-pai se preciso."""
        try:
            os.makedirs(os.path.dirname(self.lock_dir), exist_ok=True)
        except Exception:  # noqa: BLE001
            pass
        deadline = _now_ms() + self.acquire_timeout_ms
        while True:
            try:
                os.mkdir(self.lock_dir)     # atômico: falha se já existe
                self._write_meta()
                self.acquired = True
                return True
            except FileExistsError:
                if self._is_stale():
                    self._steal()           # lock órfão: tenta roubar
                    if os.path.isdir(self.lock_dir):
                        # roubo NÃO removeu o dir (ex.: arquivo preso no Windows):
                        # respeita deadline + backoff em vez de hot-spin (não gira
                        # sem dormir). Retenta o mkdir após o sleep.
                        if _now_ms() >= deadline:
                            return False
                        time.sleep(self.poll_ms / 1000.0)
                    continue                # dir removido ⇒ retenta o mkdir de imediato
                if _now_ms() >= deadline:
                    return False
                time.sleep(self.poll_ms / 1000.0)
            except OSError:
                if _now_ms() >= deadline:
                    return False
                time.sleep(self.poll_ms / 1000.0)

    def release(self) -> None:
        """Libera o lock SOMENTE se ele ainda é NOSSO (``meta.pid == getpid()``).

        Se o nosso lock foi roubado (meta sumiu ou tem outro pid), NÃO removemos —
        estaríamos apagando o lock de outro processo. Idempotente, nunca levanta."""
        if not self.acquired:
            return
        self.acquired = False
        if not self._owned_by_us():
            return  # lock roubado: não mexe no dir/meta de outro dono
        try:
            os.remove(self.meta_path)
        except OSError:
            pass
        try:
            os.rmdir(self.lock_dir)
        except OSError:
            shutil.rmtree(self.lock_dir, ignore_errors=True)

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *exc):
        self.release()


# ---------------------------------------------------------------------------
# Subir o daemon INSTALADO (destacado, sem janela) + esperar o pipe.
# ---------------------------------------------------------------------------
def _spawn_daemon_outside_job(args: list) -> bool:
    """Sobe o daemon FORA do Job do consumidor via WMI ``Win32_Process.Create``.

    Quem cria o processo é o **WmiPrvSE** (serviço do WMI), então o daemon NÃO herda o
    Job Object do worker efêmero — ele SOBREVIVE ao reload/queda do consumidor. É o
    escape usado quando ``CREATE_BREAKAWAY_FROM_JOB`` é NEGADO (Job sem
    ``JOB_OBJECT_LIMIT_BREAKAWAY_OK``, ex.: host de extensão): sem isto, um simples
    DETACHED deixaria o daemon dentro do Job e o Windows o mataria junto — o clássico
    loop start↔kill que derruba a voz. Best-effort, Windows-only; devolve ``True`` só se
    o WMI reportou criação (``ReturnValue == 0`` e ``ProcessId > 0``). O ``--log-file`` do
    daemon cobre o log; só o boot-log de stdout/stderr não é capturado nesta rota."""
    try:
        cmdline = subprocess.list2cmdline(args).replace("'", "''")   # quoting Windows correto
        ps = ("$r = Invoke-CimMethod -ClassName Win32_Process -MethodName Create "
              f"-Arguments @{{ CommandLine = '{cmdline}' }}; "
              "if ($r.ReturnValue -eq 0 -and $r.ProcessId -gt 0) { exit 0 } exit 1")
        r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                           capture_output=True, text=True, timeout=25,
                           creationflags=_NO_WINDOW)
        return r.returncode == 0
    except Exception:  # noqa: BLE001 — WMI ausente/negado ⇒ o chamador cai no último recurso
        return False


def start_installed_daemon(pipe: str = DEFAULT_PIPE, *, run=None) -> bool:
    """Sobe o daemon INSTALADO destacado (``DETACHED_PROCESS | CREATE_NO_WINDOW``)
    via ``pythonw.exe -m vox_engine``. Devolve ``False`` se o motor não está
    instalado ou o lançamento falhou. stdout/err do boot vão p/ um log (captura um
    crash de import antes do ``--log-file`` do próprio daemon)."""
    pyw = INSTALLED_PYTHONW
    if not os.path.exists(pyw):
        return False
    try:
        os.makedirs(os.path.dirname(DAEMON_LOG), exist_ok=True)
        # DETACHED_PROCESS | CREATE_NO_WINDOW | CREATE_BREAKAWAY_FROM_JOB: o motor é um SINGLETON de
        # LONGA VIDA — precisa ROMPER o Job Object do consumidor que o lança (worker/sessão do app),
        # senão o Windows o MATA junto quando esse consumidor EFÊMERO cai (sem shutdown gracioso).
        # Era o loop start↔kill que derrubava a voz no multi-sessão.
        base = 0x00000008 | 0x08000000
        breakaway = 0x01000000
        bf = open(DAEMON_BOOT_LOG, "ab")   # noqa: SIM115
        try:
            args = [pyw, "-m", "vox_engine", "--pipe", pipe, "--log-file", DAEMON_LOG]
            common = dict(close_fds=True, stdin=subprocess.DEVNULL, stdout=bf, stderr=bf)
            try:
                subprocess.Popen(args, creationflags=base | breakaway, **common)
            except OSError:
                # Job PROÍBE breakaway (ERROR_ACCESS_DENIED — sem JOB_OBJECT_LIMIT_BREAKAWAY_OK).
                # Um DETACHED puro ainda deixaria o daemon DENTRO do Job do worker efêmero ⇒ morre
                # no reload/queda do consumidor. ESCAPA do Job via WMI (WmiPrvSE cria FORA do Job).
                # Só se o WMI também falhar caímos no detached (piso: nunca pior que antes).
                if not _spawn_daemon_outside_job(args):
                    subprocess.Popen(args, creationflags=base, **common)
        finally:
            bf.close()
        return True
    except Exception:  # noqa: BLE001 — lançamento falhou
        return False


def _wait_for_pipe(pipe: str, boot_timeout: float, *,
                   auto_reconnect: bool = False,
                   reconnect_timeout: float = 0.25) -> "VoxClient | None":
    """Espera o pipe subir (poll) até ``boot_timeout`` e devolve o cliente, ou
    ``None`` se não apareceu (provável crash de import no daemon)."""
    deadline = time.time() + max(0.0, boot_timeout)
    while time.time() < deadline:
        c = VoxClient.try_connect(pipe, connect_timeout=1.0, auto_reconnect=auto_reconnect,
                                  reconnect_timeout=reconnect_timeout)
        if c is not None:
            return c
        time.sleep(0.5)
    return None


# ---------------------------------------------------------------------------
# stop_daemon — reciclagem: derruba um daemon rodando para o update valer (e o
# hook do daemon subir o ditado com a versão nova). Paridade com o SDK Node.
# ---------------------------------------------------------------------------
def _pidfile_path(pipe: str) -> str:
    """Pidfile que o daemon escreve no boot (chaveado pelo pipe; espelha
    ``__main__._pidfile_path``). sha1(pipe)[:12] — derivação idêntica no daemon."""
    import hashlib
    digest = hashlib.sha1(pipe.encode("utf-8")).hexdigest()[:12]
    return os.path.join(INSTALL_ROOT, "run", f"daemon-{digest}.pid")


def _read_pidfile(pipe: str) -> "int | None":
    try:
        with open(_pidfile_path(pipe), encoding="utf-8") as f:
            pid = int(f.read().strip())
        return pid if pid > 0 else None
    except Exception:  # noqa: BLE001
        return None


def _daemon_match_regex(pipe: str) -> str:
    """Regex p/ casar o argumento ``--pipe <pipe>`` como TOKEN ancorado (seguido de
    espaço/fim), para que ``\\.\\pipe\\vox`` nunca case um ``\\.\\pipe\\vox2`` vizinho."""
    import re as _re
    return r"--pipe\s+" + _re.escape(pipe) + r"(\s|$)"


def _find_daemon_pid(pipe: str) -> "int | None":
    """Último recurso p/ daemon LEGADO (sem pidfile, sem pid no info): acha o processo
    rodando ``-m vox_engine --pipe <pipe>`` (token ANCORADO) e devolve o PID. Windows-only."""
    try:
        esc = pipe.replace("'", "''")
        ps = ("$re = '--pipe\\s+' + [regex]::Escape('" + esc + "') + '(\\s|$)'; "
              "Get-CimInstance Win32_Process -Filter \"Name='pythonw.exe' OR "
              "Name='python.exe'\" | Where-Object { $_.CommandLine -like '*vox_engine*' "
              "-and $_.CommandLine -match $re } | "
              "Select-Object -First 1 -ExpandProperty ProcessId")
        r = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                           capture_output=True, text=True, timeout=15,
                           creationflags=_NO_WINDOW)
        pid = int((r.stdout or "").strip())
        return pid if pid > 0 else None
    except Exception:  # noqa: BLE001
        return None


def _daemon_pid_matches(pid: int, pipe: str) -> bool:
    """True sse ``pid`` É um daemon vox_engine NESTE pipe exato — p/ nunca matar um PID
    reciclado por um processo alheio (pidfile obsoleto)."""
    try:
        esc = pipe.replace("'", "''")
        ps = ("$re = '--pipe\\s+' + [regex]::Escape('" + esc + "') + '(\\s|$)'; "
              f"$p = Get-CimInstance Win32_Process -Filter \"ProcessId={int(pid)}\"; "
              "if ($p -and $p.CommandLine -like '*vox_engine*' -and "
              "$p.CommandLine -match $re) { 'yes' }")
        r = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                           capture_output=True, text=True, timeout=15,
                           creationflags=_NO_WINDOW)
        return (r.stdout or "").strip() == "yes"
    except Exception:  # noqa: BLE001
        return False


def _kill_pid(pid: int) -> None:
    """Mata o processo ``pid`` e a árvore (taskkill /T /F). Best-effort, nunca levanta."""
    try:
        subprocess.run(["taskkill", "/PID", str(int(pid)), "/T", "/F"],
                       stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, timeout=30,
                       creationflags=_NO_WINDOW)
    except Exception:  # noqa: BLE001
        pass


def _daemon_up(pipe: str) -> bool:
    c = VoxClient.try_connect(pipe, connect_timeout=0.4)
    if c is None:
        return False
    try:
        c.close()
    except Exception:  # noqa: BLE001
        pass
    return True


def _wait_pipe_down(pipe: str, timeout: float, *, is_up, monotonic, sleep) -> bool:
    deadline = monotonic() + timeout
    while True:
        try:
            up = is_up(pipe)
        except Exception:  # noqa: BLE001 — erro na sonda ⇒ assume ainda no ar
            up = True
        if not up:
            return True
        if monotonic() >= deadline:
            return False
        sleep(0.5)


def stop_daemon(pipe: str = DEFAULT_PIPE, *, pid: "int | None" = None,
                connect=None, kill=None, is_up=None, read_pidfile=None,
                find_pid=None, pid_matches=None, monotonic=None, sleep=None,
                graceful_timeout: float = 8.0, kill_timeout: float = 6.0,
                force: bool = False) -> bool:
    """Para o daemon do ``pipe`` para reciclar (o update valer + o ditado subir com a
    versão nova). 1) shutdown GRACIOSO pelo pipe (daemon 0.9.0+); se o daemon responde
    BUSY (sessão abriu na corrida) e ``force`` é False, ABORTA — nunca mata. Com ``force``
    manda ``force=True`` (o daemon derruba MESMO ocupado — uso iniciado pelo usuário). 2) espera
    o pipe cair; 3) fallback KILL por pid: explícito/info (confiável) → pidfile (VERIFICADO) →
    scan (ancorado+verificado); nunca mata um PID não verificado. 4) espera de novo. True sse o
    pipe caiu. Nunca levanta."""
    connect = connect or (lambda pp: VoxClient.try_connect(pp, connect_timeout=1.0))
    kill = kill or _kill_pid
    is_up = is_up or _daemon_up
    read_pidfile = read_pidfile or _read_pidfile
    find_pid = find_pid or _find_daemon_pid
    pid_matches = pid_matches or _daemon_pid_matches
    monotonic = monotonic or time.monotonic
    sleep = sleep or time.sleep

    learned_pid = pid   # pid explícito é confiável (responsabilidade do chamador)
    refused_busy = False
    # 1) shutdown gracioso pelo pipe (+ aprende o pid p/ o fallback).
    try:
        c = connect(pipe)
        if c is not None:
            try:
                if learned_pid is None:
                    try:
                        learned_pid = (c.info() or {}).get("pid")
                    except Exception:  # noqa: BLE001
                        learned_pid = None
                try:
                    resp, _ = c._request({"cmd": "shutdown", "force": bool(force),
                                          "req_id": c._next_rid()}, timeout=2.0)
                    if isinstance(resp, dict) and resp.get("event") == "busy":
                        refused_busy = True
                except Exception:  # noqa: BLE001 — daemon legado: bad_cmd / socket caiu
                    pass
            finally:
                try:
                    c.close()
                except Exception:  # noqa: BLE001
                    pass
    except Exception:  # noqa: BLE001
        pass

    # Daemon respondeu BUSY (sessão abriu na corrida) → NÃO mata; o chamador segue servindo.
    if refused_busy:
        return False

    # 2) espera a saída graciosa.
    if _wait_pipe_down(pipe, graceful_timeout, is_up=is_up, monotonic=monotonic, sleep=sleep):
        return True

    # 3) fallback: mata o processo EXATO (legado ou travado). Verifica pids não confiáveis.
    kill_pid = learned_pid                         # confiável: explícito ou info.pid
    if kill_pid is None:
        from_file = read_pidfile(pipe)             # pidfile: VERIFICA (pode estar obsoleto)
        if from_file is not None and pid_matches(from_file, pipe):
            kill_pid = from_file
    if kill_pid is None:
        kill_pid = find_pid(pipe)                  # scan: ancorado + auto-verificado
    if kill_pid is not None:
        try:
            kill(kill_pid)
        except Exception:  # noqa: BLE001
            pass
        if _wait_pipe_down(pipe, kill_timeout, is_up=is_up, monotonic=monotonic, sleep=sleep):
            return True
    # 4) veredito final.
    try:
        return not is_up(pipe)
    except Exception:  # noqa: BLE001
        return False


# ---------------------------------------------------------------------------
# Máquina de estados: ensure_vox / ensure_vox_detailed.
# ---------------------------------------------------------------------------
def _reuse_result(client: "VoxClient", *, auto_update: bool, http_get) -> dict:
    """Monta o resultado do caminho de REUSO (daemon já no ar): lê info.version e
    reporta ``updateAvailable`` best-effort (não perturba o daemon servindo)."""
    runtime_ver = None
    try:
        runtime_ver = (client.info() or {}).get("version")
    except Exception:  # noqa: BLE001
        runtime_ver = None
    latest = latest_release(http_get=http_get) if auto_update else None
    return {
        "client": client,
        "installedVersion": runtime_ver,
        "latestVersion": latest["version"] if latest else None,
        "updateAvailable": bool(latest and runtime_ver
                                and is_newer(latest["version"], runtime_ver)),
        "action": "reused",
    }


def ensure_vox_detailed(pipe: "str | None" = None, *, autostart: bool = True,
                        auto_update: bool = True, recycle_stale: bool = True,
                        auto_reconnect: bool = False,
                        reconnect_timeout: float = 0.25,
                        connect_timeout_ms: "int | None" = None,
                        boot_timeout_ms: "int | None" = None,
                        python_exe: "str | None" = None,
                        with_translation: bool = True,
                        http_get=None, run=None, stop=None) -> dict:
    """Install → update → use, com relatório. Devolve um dict com as MESMAS chaves
    do SDK Node: ``{client, installedVersion, latestVersion, updateAvailable, action}``.

    ``action`` ∈ ``reused | installed | updated | up_to_date | offline_installed |
    failed | unavailable``.

    ``auto_reconnect`` (opt-in): o :class:`VoxClient` devolvido se auto-cura — reabre o
    handle quando o motor cai/recicla e repete o request só na falha de escrita (nunca
    em leitura/timeout). O consumidor não reimplementa reconexão.

    Estados:
      1. **pipe no ar** ⇒ devolve o cliente conectado (``reused``) + sinaliza
         ``updateAvailable`` comparando ``info.version`` com a release — NÃO baixa,
         NÃO derruba a sessão viva.
      2. **pipe fora** ⇒ re-checa (outro pode ter subido/instalado) e decide:
         ausente/velho ⇒ baixa+VERIFICA(fail-closed)+``install.ps1 -NoStart`` (o LOCK
         de instalação vive DENTRO de ``download_and_run_installer`` — FIX 4);
         offline+instalado ⇒ usa o instalado; offline+ausente ⇒ ``unavailable``.
      3. sobe o daemon instalado e espera o pipe (boot generoso). Falha ⇒ ``failed``.
    """
    pipe = pipe or DEFAULT_PIPE
    stop = stop or stop_daemon
    connect_timeout = (connect_timeout_ms if connect_timeout_ms is not None
                       else CONNECT_TIMEOUT_MS) / 1000.0
    boot_timeout = (boot_timeout_ms if boot_timeout_ms is not None
                    else BOOT_TIMEOUT_MS) / 1000.0
    result = {"client": None, "installedVersion": None, "latestVersion": None,
              "updateAvailable": False, "action": "unavailable"}

    # ---- (1) daemon já no ar: reusar OU reciclar (se velho e ocioso) ----
    client = VoxClient.try_connect(pipe, connect_timeout, auto_reconnect=auto_reconnect,
                                   reconnect_timeout=reconnect_timeout)
    if client is not None:
        running_ver = running_sessions = running_pid = None
        try:
            info = client.info() or {}
            running_ver = info.get("version")
            running_sessions = info.get("sessions")
            running_pid = info.get("pid")
        except Exception:  # noqa: BLE001
            pass
        latest = latest_release(http_get=http_get) if auto_update else None
        latest_ver = latest["version"] if latest else None
        stale = bool(latest and running_ver and is_newer(latest_ver, running_ver))

        # RECICLA um daemon VELHO para o update valer e o hook do daemon subir o ditado
        # com a versão nova — só quando é SEGURO: dá p/ subir (autostart), está OCIOSO
        # (sessions == 0) e o venv arranca (installed != None). ``installed_version`` gera
        # um subprocesso, então é avaliado POR ÚLTIMO (só quando o resto já qualifica) —
        # mantém o caminho comum "atual → reusa" sem spawn desnecessário.
        idle = running_sessions == 0
        if (stale and recycle_stale and autostart and idle
                and installed_version(python_exe, run=run) is not None):
            try:
                client.close()
            except Exception:  # noqa: BLE001
                pass
            try:
                stopped = stop(pipe, pid=running_pid)
            except Exception:  # noqa: BLE001 — stop é best-effort; nunca quebra o ensure
                stopped = False
            if stopped:
                client = None   # cai no (2)/(3): instala a última + sobe o daemon novo
            else:
                re = VoxClient.try_connect(pipe, connect_timeout,
                                           auto_reconnect=auto_reconnect,
                                           reconnect_timeout=reconnect_timeout)
                return {"client": re, "installedVersion": running_ver,
                        "latestVersion": latest_ver, "updateAvailable": True,
                        "action": "reused"}
        else:
            return {"client": client, "installedVersion": running_ver,
                    "latestVersion": latest_ver, "updateAvailable": stale,
                    "action": "reused"}

    if not autostart:
        result["action"] = "unavailable"
        return result

    # ---- (2) re-checar + decidir + instalar ----
    # FIX 4: NÃO há lock externo aqui — o mesmo processo não pode dar mkdir no mesmo
    # dir duas vezes (deadlock). O lock cross-process vive DENTRO de
    # download_and_run_installer, que também re-checa versão-alvo sob o lock.
    # RE-CHECAR: outro cliente pode ter subido o daemon enquanto conectávamos.
    # RE-CHECAR: outro cliente pode ter subido o daemon enquanto conectávamos. Propaga
    # auto_reconnect/cushion — senão o cliente 'reused' desta corrida perde o self-healing.
    client = VoxClient.try_connect(pipe, min(connect_timeout, 2.0),
                                   auto_reconnect=auto_reconnect,
                                   reconnect_timeout=reconnect_timeout)
    if client is not None:
        return _reuse_result(client, auto_update=auto_update, http_get=http_get)

    latest = latest_release(http_get=http_get) if auto_update else None
    result["latestVersion"] = latest["version"] if latest else None
    cur = installed_version(python_exe, run=run)

    need_install = latest is not None and (cur is None or is_newer(latest["version"], cur))

    if need_install:
        ok = download_and_run_installer(latest["asset_url"], latest.get("sig_url"),
                                        http_get=http_get, run=run, pipe=pipe,
                                        target_version=latest["version"],
                                        python_exe=python_exe,
                                        extra_args=(["-WithTranslation"] if with_translation else None))
        if not ok:
            # verify/download/install/lock falho (fail-closed): usa o existente.
            if cur is None:
                result["action"] = "failed"
                return result
            action, cur_ver = "offline_installed", cur
        else:
            new = installed_version(python_exe, run=run)
            if new is None:
                # install quebrado: NÃO sobe lixo.
                result["action"] = "failed"
                return result
            action = "installed" if cur is None else "updated"
            cur_ver = new
    elif latest is not None:
        action, cur_ver = "up_to_date", cur   # instalado e atual
    else:
        # Sem release (offline ou auto_update=False).
        if cur is None:
            result["action"] = "unavailable"
            return result
        action, cur_ver = "offline_installed", cur

    result["installedVersion"] = cur_ver

    # ---- (3) subir o daemon instalado + esperar o pipe ----
    if not start_installed_daemon(pipe, run=run):
        result["action"] = "failed"
        return result
    client = _wait_for_pipe(pipe, boot_timeout, auto_reconnect=auto_reconnect,
                            reconnect_timeout=reconnect_timeout)
    if client is None:
        result["action"] = "failed"
        return result
    result["client"] = client
    result["action"] = action
    try:
        rv = (client.info() or {}).get("version")
        if rv:
            result["installedVersion"] = rv
    except Exception:  # noqa: BLE001
        pass
    return result


def ensure_vox(pipe: "str | None" = None, **opts) -> "VoxClient | None":
    """Instala/atualiza/usa o motor e devolve um :class:`VoxClient` conectado, ou
    ``None`` para o chamador cair no seu fallback. Açúcar sobre
    :func:`ensure_vox_detailed` (mesmos ``opts``)."""
    return ensure_vox_detailed(pipe, **opts)["client"]


# ---------------------------------------------------------------------------
# update_engine — ENDPOINT EXPLÍCITO de atualização a mando do consumidor.
# ---------------------------------------------------------------------------
def update_engine(pipe: "str | None" = None, *, force: bool = False,
                  with_translation: bool = True, boot_timeout_ms: "int | None" = None,
                  http_get=None, python_exe: "str | None" = None, stop=None, run=None) -> dict:
    """Atualiza o motor para a ÚLTIMA release ASSINADA AGORA, a mando do consumidor
    (voice-chat/Action chamam ``client.update()``). É o mesmo recycle da auto-cura, porém
    DISPARADO sob demanda: reusa ``stop_daemon`` (gracioso) + ``check_and_update`` (baixa+VERIFICA
    fail-closed + ``install.ps1 -NoStart`` sob lock cross-process) + ``start_installed_daemon``
    (o ditado sobe com a versão nova).

    ``force=True`` recicla MESMO com sessão aberta (uso iniciado pelo usuário: aceita o breve
    reinício); sem ``force``, RECUSA quando o motor está ocupado (``busy``).

    Retorna ``{'action': <estado>, 'from': <ver|None>, 'to': <ver|None>[, 'sessions']}`` onde
    ``action`` ∈ ``up_to_date`` | ``updated`` | ``busy`` | ``no_release`` | ``failed``.
    Nunca levanta (best-effort — sempre tenta deixar um motor no ar no fim)."""
    pipe = pipe or DEFAULT_PIPE
    stop = stop or stop_daemon
    boot_timeout = (boot_timeout_ms if boot_timeout_ms is not None else BOOT_TIMEOUT_MS) / 1000.0

    latest = latest_release(http_get=http_get)
    to = latest["version"] if latest else None

    c = VoxClient.try_connect(pipe, CONNECT_TIMEOUT_MS / 1000.0)
    running_ver = running_sessions = running_pid = None
    if c is not None:
        try:
            info = c.info() or {}
            running_ver = info.get("version")
            running_sessions = info.get("sessions")
            running_pid = info.get("pid")
        except Exception:  # noqa: BLE001
            pass
    cur = running_ver or installed_version(python_exe, run=run)

    if to is None:                                   # sem release (offline)
        if c is not None:
            try:
                c.close()
            except Exception:  # noqa: BLE001
                pass
        return {"action": "no_release", "from": cur, "to": None}

    if cur and not is_newer(to, cur):                # já na última
        if c is None:
            start_installed_daemon(pipe, run=run)
        else:
            try:
                c.close()
            except Exception:  # noqa: BLE001
                pass
        return {"action": "up_to_date", "from": cur, "to": to}

    if c is not None:                                # motor no ar e velho → reciclar
        if running_sessions not in (0, None) and not force:
            try:
                c.close()
            except Exception:  # noqa: BLE001
                pass
            return {"action": "busy", "from": cur, "to": to, "sessions": running_sessions}
        try:
            c.close()
        except Exception:  # noqa: BLE001
            pass
        if not stop(pipe, pid=running_pid, force=force):
            return {"action": "busy", "from": cur, "to": to}

    res = check_and_update(python_exe, http_get=http_get, run=run, latest=latest,
                           pipe=pipe, with_translation=with_translation)
    if res.get("action") in ("failed", "unavailable"):
        start_installed_daemon(pipe, run=run)        # não deixa o consumidor sem motor
        return {"action": "failed", "from": cur, "to": to}
    if not start_installed_daemon(pipe, run=run):
        return {"action": "failed", "from": cur, "to": to}
    nc = _wait_for_pipe(pipe, boot_timeout)
    if nc is not None:
        try:
            nc.close()
        except Exception:  # noqa: BLE001
            pass
        return {"action": "updated", "from": cur, "to": to}
    return {"action": "failed", "from": cur, "to": to, "reason": "boot_timeout"}
