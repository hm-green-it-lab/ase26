# clock_drift.py
"""
Check clock drift between local machine and a remote host via SSH.
"""

# ──────────────────────────────────────────────────────────────────────────────
# Imports
# ──────────────────────────────────────────────────────────────────────────────

from __future__ import annotations

import datetime
from typing import Tuple
import paramiko


# ──────────────────────────────────────────────────────────────────────────────
# Internal helpers
# ──────────────────────────────────────────────────────────────────────────────

def _exec_remote_epoch(ssh: paramiko.SSHClient) -> Tuple[int, str]:
    """
    Execute a remote command to obtain the current epoch time (seconds since 1970-01-01).

    Returns
    -------
    Tuple[int, str]
        (remote_epoch_seconds, raw_stdout_string)

    Raises
    ------
    RuntimeError
        If the remote command fails or returns a non-numeric result.
    """
    # Using POSIX `date +%s` for integer epoch seconds.
    stdin, stdout, stderr = ssh.exec_command("date +%s")
    # Block until command finishes to get an exit status reliably.
    exit_status = stdout.channel.recv_exit_status()
    raw_out = stdout.read().decode(errors="replace").strip()
    raw_err = stderr.read().decode(errors="replace").strip()

    if exit_status != 0:
        raise RuntimeError(f"[✗] Failed to read remote time (exit {exit_status}): {raw_err or raw_out}")

    if not raw_out.isdigit():
        raise RuntimeError(f"[✗] Unexpected response from remote clock: '{raw_out}'")

    return int(raw_out), raw_out


# ──────────────────────────────────────────────────────────────────────────────
# Public API
# ──────────────────────────────────────────────────────────────────────────────

def check_remote_clock_drift(
    hostname,
    username,
    password,
    threshold_seconds: float = 2.0,
    port: int = 22,
) -> float:
    """
    Check the absolute clock drift (in seconds) between the local system and a remote host.

    Connection is established via SSH using password authentication and the remote
    time is obtained via `date +%s`. If the drift exceeds `threshold_seconds`,
    a RuntimeError is raised. Otherwise, the function prints a short status line
    and returns the drift as a float.

    Parameters
    ----------
    hostname : str
        Remote host to connect to.
    username : str
        SSH username.
    password : str
        SSH password.
    threshold_seconds : float, optional
        Maximum acceptable absolute drift in seconds (default: 2.0).
    port : int, optional
        SSH port (default: 22).

    Returns
    -------
    float
        The absolute drift in seconds (remote - local, absolute value).

    Raises
    ------
    RuntimeError
        If the remote time cannot be read or if the drift exceeds `threshold_seconds`.
    """
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())

    try:
        ssh.connect(hostname=hostname, port=port, username=username, password=password, timeout=5)
        remote_epoch, _ = _exec_remote_epoch(ssh)
    finally:
        ssh.close()

    # Use int seconds to align with remote `date +%s`
    local_epoch = int(datetime.datetime.now().timestamp())
    drift = abs(remote_epoch - local_epoch)

    # Keep existing UX/behavior (prints + RuntimeError on excessive drift)
    if drift > threshold_seconds:
        print(f"[⚠] Clock drift too high: {drift:.2f}s. Consider local NTP sync.")
        raise RuntimeError(f"[✗] Clock drift exceeds {threshold_seconds:.2f}s — aborting run.")
    else:
        print(f"[✓] Clock drift OK: {drift:.2f}s")

    return float(drift)
