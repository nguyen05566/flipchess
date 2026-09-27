#!/usr/bin/env python3
"""Mega Arena Orchestrator for flipchess repo — runs 4 bots in parallel.

Hybrid Parallelism:
  - 1 GitHub Actions job (saves quota)
  - 4 bots run as parallel background processes
  - Each bot uses its own account + ws_capture subdir
  - Orchestrator waits for all to finish (or timeout)

Bots:
  - arena20 (arena20.py) — uses MISTBOARD_PIKAFISH_XIANGQI_PATH
  - test3 (test3.py)
  - test4 (test4.py)
  - test5 (test5.py)
"""
import os
import sys
import time
import signal
import subprocess
import threading
from pathlib import Path

# All bots to run in parallel
BOTS = [
    ("arena20", "arena20.py"),
    ("test3",   "test3.py"),
    ("test4",   "test4.py"),
    ("test5",   "test5.py"),
]

REPO_DIR = Path(__file__).resolve().parent
LOG_DIR = REPO_DIR / "mega_logs"
LOG_DIR.mkdir(exist_ok=True)

# Max runtime: configurable, default 5.5 hours
MAX_RUNTIME_SEC = int(os.environ.get("MEGA_RUNTIME_SEC", str(5 * 3600 + 30 * 60)))


def run_bot(name: str, script: str, stop_event: threading.Event):
    """Run a single bot in background. Returns when bot exits or stop event set."""
    log_path = LOG_DIR / f"{name}.log"
    print(f"[{name}] Starting → {log_path}", flush=True)

    # Each bot gets its own ws_capture subdir to avoid conflicts
    env = os.environ.copy()
    env["WS_CAPTURE_DIR"] = f"ws_capture/{name}"

    try:
        with open(log_path, "w") as logf:
            proc = subprocess.Popen(
                ["python3", "-u", script],
                cwd=str(REPO_DIR),
                env=env,
                stdout=logf,
                stderr=subprocess.STDOUT,
                preexec_fn=os.setsid,
            )

            while proc.poll() is None:
                if stop_event.is_set():
                    print(f"[{name}] Stop signal received, terminating...", flush=True)
                    try:
                        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
                    except ProcessLookupError:
                        pass
                    try:
                        proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        print(f"[{name}] Force killing...", flush=True)
                        try:
                            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                    break
                time.sleep(2)

            exit_code = proc.returncode if proc.poll() is not None else -1
            print(f"[{name}] Exited with code {exit_code}", flush=True)
            return exit_code
    except Exception as e:
        print(f"[{name}] ERROR: {e}", flush=True)
        return -1


def main():
    print(f"=== Mega Arena Orchestrator (flipchess) ===", flush=True)
    print(f"Bots to run: {len(BOTS)}", flush=True)
    for name, script in BOTS:
        print(f"  - {name} ({script})", flush=True)
    print(f"Max runtime: {MAX_RUNTIME_SEC}s ({MAX_RUNTIME_SEC/3600:.1f}h)", flush=True)
    print(f"Log dir: {LOG_DIR}", flush=True)
    print("", flush=True)

    stop_event = threading.Event()

    def signal_handler(sig, frame):
        print(f"\n[ORCH] Signal {sig} received, stopping all bots...", flush=True)
        stop_event.set()
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    # Start all bots in parallel threads
    threads = []
    for name, script in BOTS:
        t = threading.Thread(target=run_bot, args=(name, script, stop_event), daemon=True)
        t.start()
        threads.append((name, t))
        time.sleep(3)  # Stagger start to avoid login rate-limit

    print(f"\n[ORCH] All {len(BOTS)} bots started. Waiting up to {MAX_RUNTIME_SEC}s...", flush=True)

    start = time.time()
    try:
        while time.time() - start < MAX_RUNTIME_SEC:
            alive = sum(1 for _, t in threads if t.is_alive())
            if alive == 0:
                print(f"[ORCH] All bots finished.", flush=True)
                break
            elapsed = int(time.time() - start)
            print(f"[ORCH] {alive}/{len(threads)} bots still running "
                  f"(elapsed {elapsed//60}m, left {(MAX_RUNTIME_SEC-elapsed)//60}m)", flush=True)
            time.sleep(60)
        else:
            print(f"[ORCH] Max runtime reached, stopping all bots...", flush=True)
            stop_event.set()
    except KeyboardInterrupt:
        print(f"[ORCH] KeyboardInterrupt, stopping...", flush=True)
        stop_event.set()

    # Wait for threads to finish cleanup (max 30s)
    deadline = time.time() + 30
    for name, t in threads:
        remaining = max(0, deadline - time.time())
        t.join(timeout=remaining)
        if t.is_alive():
            print(f"[ORCH] WARNING: {name} still running after timeout", flush=True)

    print(f"\n[ORCH] Done. Logs in {LOG_DIR}/", flush=True)

    # Print summary
    print(f"\n=== Summary ===", flush=True)
    for name, _ in BOTS:
        log_path = LOG_DIR / f"{name}.log"
        if log_path.exists():
            size = log_path.stat().st_size
            games = 0
            try:
                with open(log_path) as f:
                    games = sum(1 for line in f if "[GAME] OVER" in line or "GAME OVER" in line)
            except:
                pass
            print(f"  {name:10s}: log={size:>8} bytes, games={games}", flush=True)
        else:
            print(f"  {name:10s}: NO LOG", flush=True)


if __name__ == "__main__":
    main()
