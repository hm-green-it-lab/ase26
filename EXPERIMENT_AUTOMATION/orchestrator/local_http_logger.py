# local_rittal_reader.py
"""
Run the local Rittal SNMP reader JAR for a fixed duration and save its output.

Behavior
--------
- Creates a daily subfolder inside `output_dir` named YYYYMMDD.
- Streams the JAR's stdout/stderr into a CSV named:
    rittal_{experiment_type}_{YYYYMMDD}_{HHMMSS}_{iteration}_{total_iterations}.csv
- Runs for `duration` seconds, then attempts a clean shutdown:
    - Windows: taskkill /F /T (kills the whole process tree)
    - POSIX  : SIGTERM, then waits up to 10s

Notes
-----
- Public function signature and overall behavior are preserved.
- No return value is introduced to avoid any breaking change.
"""

# ──────────────────────────────────────────────────────────────────────────────
# Imports
# ──────────────────────────────────────────────────────────────────────────────

from __future__ import annotations

import datetime
import os
import subprocess
import sys
import time
from typing import Optional


# ──────────────────────────────────────────────────────────────────────────────
# Public API (unchanged signature)
# ──────────────────────────────────────────────────────────────────────────────

def run_local_http_logger(
    jar_path: str,
    duration: int,
    experiment_type: str,
    iteration: int,
    total_iterations: int,
    http_logger_url: str,
    output_dir: str = "./output",
    interval: str = "*/1 * * * * ?",
) -> None:
    """
    Execute the HTTP-Logger reader JAR locally for `duration` seconds and save output.

    Parameters
    ----------
    jar_path : str
        Path to the HTTP-Logger reader JAR.
    duration : int
        Runtime in seconds before stopping the process.
    experiment_type : str
        Free-form tag included in the output filename.
    iteration : int
        Current iteration index (for filename).
    total_iterations : int
        Total number of iterations (for filename).
    output_dir : str, optional
        Root output directory (default: ./output).
    interval : str, optional
        Quartz/CRON expression for sampling interval (default: every second).

    Side Effects
    ------------
    - Writes a CSV file to {output_dir}/{YYYYMMDD}/local_http_logger{...}.csv
    - Prints status lines to stdout.
    """
    # ── Basic guards (kept soft to avoid behavior changes) ────────────────────
    if not os.path.isfile(jar_path):
        print(f"[!] JAR not found: {jar_path} (continuing; Java will likely fail)")
    if duration <= 0:
        print(f"[!] Non-positive duration ({duration}); will stop immediately.")

    # ── Naming & folders ──────────────────────────────────────────────────────
    now = datetime.datetime.now()
    date_tag = now.strftime("%Y%m%d")
    time_tag = now.strftime("%H%M%S")

    os.makedirs(output_dir, exist_ok=True)

    filename = f"http_logger_{experiment_type}_{date_tag}_{time_tag}_{iteration}_{total_iterations}.csv"
    output_path = os.path.join(output_dir, filename)

    # ── Launch ────────────────────────────────────────────────────────────────
    creationflags: Optional[int] = 0
    if sys.platform == "win32":
        # Create a new process group so taskkill /T can terminate descendants.
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]

    java_cmd = [
        "java",
        "-XX:+DisableAttachMechanism",
        f"-Dhttplogger.cron={interval}",
        "-jar",
        jar_path,
        http_logger_url
    ]

    with open(output_path, "w", buffering=1) as outfile:  # line-buffered
        try:
            proc = subprocess.Popen(
                java_cmd,
                stdout=outfile,
                stderr=subprocess.STDOUT,
                creationflags=creationflags or 0,
            )
        except Exception as e:
            print(f"[!] Failed to start http-logger: {type(e).__name__}: {e}")
            return

        print(f"[~] Start local http-logger (PID: {proc.pid})")
        print(f"[→] Writing to: {output_path}")

        # ── Run for the requested duration ────────────────────────────────────
        try:
            time.sleep(max(0, duration))
        except KeyboardInterrupt:
            print("[!] Interrupted by user (Ctrl+C). Stopping reader...")

        # ── Stop logic ────────────────────────────────────────────────────────
        try:
            if sys.platform == "win32":
                # /T = terminate child processes, /F = force
                subprocess.run(
                    ["taskkill", "/PID", str(proc.pid), "/F", "/T"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                )
                print(f"[~] Stop local http-logger (PID: {proc.pid})")
            else:
                proc.terminate()  # SIGTERM
            # Give the process a moment to flush and exit
            proc.wait(timeout=10)
        except Exception as e:
            print(f"[!] Failed to stop http-logger: {e}")
        finally:
            # Double-check process is gone; avoid zombies on POSIX
            if proc.poll() is None:
                try:
                    if sys.platform != "win32":
                        proc.kill()
                        proc.wait(timeout=5)
                except Exception:
                    pass

        # Mirror the Windows info message on POSIX for consistency
        print(f"[↓] Save local http-logger to {filename}")
