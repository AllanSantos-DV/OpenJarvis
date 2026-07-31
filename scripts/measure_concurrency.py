"""Fase 0, assert -1a e -1b: o que custa rodar sessoes headless em paralelo.

Decide arquitetura: se cada sessao sobe um runtime inteiro (Pattern 1), a
concorrencia nao cabe na RAM desta maquina e o maestro tem que ser fila. Se
compartilham (Pattern 2), paralelismo e viavel.

Mede: processos e RSS antes/durante, e wall-clock com N=1,2,3.
Nao escreve em sessao nenhuma -- so abre filhos com um prompt trivial.
"""

from __future__ import annotations

import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor

import psutil

PROMPT = "Responda somente: ok"


def snapshot():
    """Processos copilot/node vivos e o RSS somado, em MB."""
    procs = []
    for p in psutil.process_iter(["name", "memory_info"]):
        try:
            name = (p.info["name"] or "").lower()
            if "copilot" in name or name == "node.exe":
                procs.append((p.pid, name, p.info["memory_info"].rss / 1024 / 1024))
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return procs


def _binary() -> str:
    """Absolute path to the CLI.

    Passing "copilot" as argv[0] fails on Windows with WinError 2: it is an npm
    shim (copilot.cmd), and CreateProcess does not apply PATHEXT. Known bug in
    this project, met again here.
    """
    from openjarvis.agents.copilot_cli import _resolve_binary

    return _resolve_binary() or "copilot"


def run_one(index: int) -> float:
    started = time.monotonic()
    subprocess.run(
        [_binary(), "-p", PROMPT, "--no-ask-user", "--no-color"],
        capture_output=True,
        text=True,
        timeout=300,
    )
    return time.monotonic() - started


def measure(n: int) -> dict:
    before = snapshot()
    peak = {"procs": len(before), "rss": sum(p[2] for p in before)}

    def watch():
        deadline = time.monotonic() + 300
        while not done["yes"] and time.monotonic() < deadline:
            now = snapshot()
            peak["procs"] = max(peak["procs"], len(now))
            peak["rss"] = max(peak["rss"], sum(p[2] for p in now))
            time.sleep(0.5)

    done = {"yes": False}
    started = time.monotonic()
    with ThreadPoolExecutor(max_workers=n + 1) as pool:
        watcher = pool.submit(watch)
        times = list(pool.map(run_one, range(n)))
        done["yes"] = True
        watcher.result()
    wall = time.monotonic() - started

    return {
        "n": n,
        "wall": round(wall, 1),
        "slowest": round(max(times), 1),
        "procs_before": len(before),
        "procs_peak": peak["procs"],
        "rss_before_mb": round(sum(p[2] for p in before)),
        "rss_peak_mb": round(peak["rss"]),
    }


if __name__ == "__main__":
    ns = [int(a) for a in sys.argv[1:]] or [1, 2, 3]
    results = []
    for n in ns:
        print(f"--- N={n} ---", flush=True)
        r = measure(n)
        results.append(r)
        print(
            f"  wall={r['wall']}s  mais_lento={r['slowest']}s\n"
            f"  processos {r['procs_before']} -> pico {r['procs_peak']} "
            f"(+{r['procs_peak'] - r['procs_before']})\n"
            f"  RSS {r['rss_before_mb']}MB -> pico {r['rss_peak_mb']}MB "
            f"(+{r['rss_peak_mb'] - r['rss_before_mb']}MB)",
            flush=True,
        )
        time.sleep(3)

    print("\n=== VEREDITO ===")
    base = results[0]
    for r in results[1:]:
        extra_procs = r["procs_peak"] - base["procs_peak"]
        extra_rss = r["rss_peak_mb"] - base["rss_peak_mb"]
        linear = base["wall"] * r["n"]
        print(
            f"N={r['n']}: +{extra_procs} processos, +{extra_rss}MB, "
            f"wall {r['wall']}s vs {round(linear, 1)}s se fosse serial "
            f"({round(r['wall'] / linear * 100)}% do serial)"
        )
