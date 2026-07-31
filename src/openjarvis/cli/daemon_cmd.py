"""``jarvis start|stop|restart|status`` — daemon management commands."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time

import click
from rich.console import Console

from openjarvis.core.config import DEFAULT_CONFIG_DIR, load_config

_PID_FILE = DEFAULT_CONFIG_DIR / "server.pid"
_LOG_FILE = DEFAULT_CONFIG_DIR / "server.log"


def _read_pid() -> int | None:
    """Read PID from pid file, return None if not found or stale."""
    if not _PID_FILE.exists():
        return None
    try:
        pid = int(_PID_FILE.read_text().strip())
        # Check if process is still running
        os.kill(pid, 0)
        return pid
    except (ValueError, OSError):
        _PID_FILE.unlink(missing_ok=True)
        return None


def _write_pid(pid: int) -> None:
    """Write PID to pid file."""
    DEFAULT_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    _PID_FILE.write_text(str(pid))


def _clear_pid() -> None:
    """Drop the pid file after a failed start, so it never points at nothing."""
    _PID_FILE.unlink(missing_ok=True)


@click.group()
def daemon() -> None:
    """Manage the OpenJarvis server daemon."""


@daemon.command()
@click.option("--host", default=None, help="Bind address.")
@click.option("--port", default=None, type=int, help="Port number.")
@click.option("-e", "--engine", "engine_key", default=None, help="Engine backend.")
@click.option("-m", "--model", "model_name", default=None, help="Default model.")
@click.option("-a", "--agent", "agent_name", default=None, help="Agent type.")
def start(
    host: str | None,
    port: int | None,
    engine_key: str | None,
    model_name: str | None,
    agent_name: str | None,
) -> None:
    """Start the OpenJarvis server as a background daemon."""
    console = Console(stderr=True)

    existing = _read_pid()
    if existing is not None:
        console.print(f"[yellow]Server already running (PID {existing}).[/yellow]")
        console.print("Use 'jarvis stop' to stop it first, or 'jarvis restart'.")
        sys.exit(1)

    config = load_config()
    bind_host = host or config.server.host
    bind_port = port or config.server.port

    # Build command to run jarvis serve
    cmd = [sys.executable, "-m", "openjarvis.cli", "serve"]
    if host:
        cmd.extend(["--host", host])
    if port:
        cmd.extend(["--port", str(port)])
    if engine_key:
        cmd.extend(["--engine", engine_key])
    if model_name:
        cmd.extend(["--model", model_name])
    if agent_name:
        cmd.extend(["--agent", agent_name])

    # Start as background process, fully detached from the launching terminal.
    #
    # ``start_new_session`` is POSIX-only: CPython's Windows ``_execute_child``
    # names the parameter ``unused_start_new_session`` and ignores it. Relying
    # on it there leaves the server sharing its parent's console, so closing
    # that console — or logging off — delivers CTRL_CLOSE_EVENT and kills the
    # daemon. DETACHED_PROCESS gives it no console at all; the new process
    # group additionally stops a Ctrl-C in the parent reaching it.
    DEFAULT_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    log_fh = open(_LOG_FILE, "a")  # noqa: SIM115
    spawn_kwargs: dict = {}
    if sys.platform == "win32":
        spawn_kwargs["creationflags"] = (
            subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
        )
    else:
        spawn_kwargs["start_new_session"] = True
    proc = subprocess.Popen(
        cmd,
        stdout=log_fh,
        stderr=log_fh,
        **spawn_kwargs,
    )
    _write_pid(proc.pid)

    problem = _wait_until_serving(proc, bind_host, bind_port)
    if problem:
        _clear_pid()
        console.print(f"[red]O servidor nao subiu.[/red]\n{problem}")
        console.print(f"  Log completo: {_LOG_FILE}")
        sys.exit(1)

    console.print(
        f"[green]OpenJarvis server started[/green] (PID {proc.pid})\n"
        f"  URL: http://{bind_host}:{bind_port}\n"
        f"  Log: {_LOG_FILE}"
    )


#: Cold start imports the whole app graph; this is generous enough for a slow
#: machine and short enough that a real failure is not mistaken for slowness.
_BOOT_TIMEOUT = 45.0

#: Import errors the server dies on, mapped to what actually fixes them. A
#: traceback in a log file the owner never opens is the same as no message.
_KNOWN_CAUSES = (
    (
        "python-multipart",
        "Falta a dependencia python-multipart. Instale os extras do servidor:\n"
        # The brackets are escaped for rich, which would otherwise read
        # `[server]` as markup and silently drop the very part that matters.
        '  uv pip install "openjarvis\\[server]"',
    ),
    (
        "No module named 'polars'",
        "Falta o polars (usado pelos relatorios). Instale com:\n"
        "  uv pip install polars",
    ),
    (
        "Address already in use",
        "A porta ja esta ocupada. Use --port outra, ou pare o processo que a usa.",
    ),
)


def _wait_until_serving(proc, host: str, port: int) -> str:
    """Block until the daemon answers, and explain it if it never does.

    ``Popen`` returning is not the server working: it dies moments later on a
    missing import, having already printed "started" with a pid. The owner then
    has a pid file pointing at nothing and no idea why -- which is exactly how a
    missing ``python-multipart`` cost a day here.

    The check is bound to the process we spawned, not merely to the port. An
    earlier run that outlived its pid file still answers on that port, and a
    port-only probe reports success while OUR process is already dead -- measured
    here, the first version of this check did exactly that.

    Returns "" when the server is up, or a message naming the cause.
    """
    import time
    import urllib.error
    import urllib.request

    url = f"http://{host}:{port}/docs"
    deadline = time.monotonic() + _BOOT_TIMEOUT

    while time.monotonic() < deadline:
        if proc.poll() is not None:
            return _explain_log()
        answered = False
        try:
            with urllib.request.urlopen(url, timeout=2):
                answered = True
        except urllib.error.HTTPError:
            # Any HTTP answer means an app is serving; the path may not exist.
            answered = True
        except Exception:  # noqa: BLE001 -- not up yet
            time.sleep(0.5)

        if answered:
            # Something is serving -- but is it ours? Give a dying child a
            # moment to actually die, then insist it is still alive.
            time.sleep(1.0)
            if proc.poll() is not None:
                return (
                    "Outro processo ja responde nessa porta, e o servidor que "
                    "acabou de subir morreu.\n" + _explain_log()
                )
            return ""

    if proc.poll() is not None:
        return _explain_log()
    return (
        f"O processo continua vivo mas nao respondeu em {int(_BOOT_TIMEOUT)}s.\n"
        "Veja o log para saber em que ponto ele parou."
    )


def _explain_log() -> str:
    """Turn the tail of the log into something worth reading."""
    try:
        tail = _LOG_FILE.read_text(encoding="utf-8", errors="replace")[-4000:]
    except OSError:
        return "O processo morreu e o log nao pode ser lido."

    for needle, advice in _KNOWN_CAUSES:
        if needle in tail:
            return advice

    lines = [line for line in tail.splitlines() if line.strip()]
    last = "\n".join(f"  {line}" for line in lines[-6:])
    return f"O processo morreu logo apos iniciar. Fim do log:\n{last}"


@daemon.command()
def stop() -> None:
    """Stop the running OpenJarvis server daemon."""
    console = Console(stderr=True)
    pid = _read_pid()
    if pid is None:
        console.print("[yellow]No running server found.[/yellow]")
        sys.exit(1)

    try:
        os.kill(pid, signal.SIGTERM)
        # Wait up to 10 seconds for graceful shutdown
        for _ in range(20):
            time.sleep(0.5)
            try:
                os.kill(pid, 0)
            except OSError:
                break
        else:
            # Force kill if still running
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
    except OSError:
        pass

    _PID_FILE.unlink(missing_ok=True)
    console.print(f"[green]Server stopped[/green] (PID {pid}).")


@daemon.command()
@click.pass_context
def restart(ctx: click.Context) -> None:
    """Restart the OpenJarvis server daemon."""
    console = Console(stderr=True)
    pid = _read_pid()
    if pid is not None:
        console.print(f"Stopping server (PID {pid})...")
        ctx.invoke(stop)
    ctx.invoke(start)


@daemon.command()
def status() -> None:
    """Show status of the OpenJarvis server daemon."""
    console = Console(stderr=True)
    pid = _read_pid()
    if pid is None:
        console.print("[yellow]Server is not running.[/yellow]")
        return

    # Get process info
    uptime_info = ""
    try:
        import psutil

        proc = psutil.Process(pid)
        uptime = time.time() - proc.create_time()
        hours, remainder = divmod(int(uptime), 3600)
        minutes, seconds = divmod(remainder, 60)
        uptime_info = f"\n  Uptime: {hours}h {minutes}m {seconds}s"
    except (ImportError, Exception):
        pass

    config = load_config()
    console.print(
        f"[green]Server is running[/green] (PID {pid}){uptime_info}\n"
        f"  URL: http://{config.server.host}:{config.server.port}\n"
        f"  Log: {_LOG_FILE}"
    )


__all__ = ["daemon", "start", "stop", "restart", "status"]
