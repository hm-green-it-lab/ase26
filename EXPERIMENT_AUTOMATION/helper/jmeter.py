# helper/jmeter.py
"""
Remote JMeter runner, fetcher, and shutdown helpers.

Highlights
----------
- Filenames follow the common pattern:
    {tool}_{experiment_type}_{YYYYMMDD_%H%M%S}_{iter}_{total}.{ext}
  e.g., jmeter_teastore_tomcat_idle_20250819_110131_1_3.jtl

- Public function signatures and behavior are unchanged (call sites unaffected).
"""

from __future__ import annotations

import os
import re
import time
import shlex
import posixpath
from datetime import datetime
from typing import List, Tuple

import paramiko


# ──────────────────────────────────────────────────────────────────────────────
# Utilities
# ──────────────────────────────────────────────────────────────────────────────

def _ts(fmt: str = "%Y%m%d_%H%M%S") -> str:
    """Return current local time formatted as string."""
    return datetime.now().strftime(fmt)


def _normalize_posix(p: str) -> str:
    """Normalize a path to POSIX style (forward slashes)."""
    return re.sub(r'\\+', '/', str(p)).strip()


def _sanitize_name(s: str) -> str:
    """Restrict a string to [A-Za-z0-9_], collapsing others to underscores."""
    return re.sub(r'[^A-Za-z0-9_]+', '_', str(s)).strip('_')


def _build_standard_filename(
    tool: str,
    experiment_type: str,
    dt_str: str,
    iteration: int,
    total: int,
    ext: str,
) -> str:
    """
    Build a standardized filename used across tools.

    Example
    -------
    >>> _build_standard_filename("jmeter", "teastore_tomcat_idle", "20250819_110131", 1, 3, ".jtl")
    'jmeter_teastore_tomcat_idle_20250819_110131_1_3.jtl'
    """
    return (
        f"{_sanitize_name(tool)}_"
        f"{_sanitize_name(experiment_type)}_"
        f"{dt_str}_{int(iteration)}_{int(total)}{ext}"
    )


def _connect(host: str, user: str, pwd: str) -> paramiko.SSHClient:
    """Create and return an SSH connection (password auth)."""
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(host, username=user, password=pwd)
    return c


def _sftp(host: str, user: str, pwd: str) -> tuple[paramiko.SSHClient, paramiko.SFTPClient]:
    """Open SSH + SFTP; return (ssh_client, sftp_client)."""
    c = _connect(host, user, pwd)
    return c, c.open_sftp()


# ──────────────────────────────────────────────────────────────────────────────
# Command builder
# ──────────────────────────────────────────────────────────────────────────────

def build_jmeter_command(jm: dict, results_path: str, jmeter_log_path: str | None = None) -> str:
    """
    Build a non-GUI JMeter command line with optional -J properties, per-run -j log,
    and heap size via env (JVM_ARGS/HEAP) based on jm['heap_xms'] / jm['heap_xmx'].
    """
    bin_path = jm.get("bin_path")
    test_plan = jm.get("test_plan")
    if not bin_path or not test_plan:
        raise RuntimeError("[JMeter] Missing required jmeter.bin_path / test_plan in config.")

    # ---- heap / JVM args from config
    heap_xms = (jm.get("heap_xms") or "").strip()
    heap_xmx = (jm.get("heap_xmx") or "").strip()
    extra_jvm = (jm.get("extra_jvm_args") or "").strip()

    jvm_parts: list[str] = []
    if heap_xms:
        jvm_parts.append(f"-Xms{heap_xms}")
    if heap_xmx:
        jvm_parts.append(f"-Xmx{heap_xmx}")
    if extra_jvm:
        jvm_parts.append(extra_jvm)

    env_prefix = ""
    if jvm_parts:
        jvm_str = " ".join(jvm_parts)
        # Set both for compatibility with various jmeter.sh/jmeter.bat wrappers
        env_bits = [
            f'JAVA_HOME=/usr/lib/jvm/java-21-openjdk-amd64 ',
            f'JVM_ARGS={shlex.quote(jvm_str)}',
            f'HEAP={shlex.quote(jvm_str)}'
        ]
        env_prefix = " ".join(env_bits) + " "

    # ---- -J properties
    prop_args = []
    for k, v in (jm.get("props") or {}).items():
        if isinstance(v, bool):
            v = "true" if v else "false"
        prop_args.append(f"-J{str(k)}={str(v)}")

    # ---- final command (env prefix + binary + args)
    cmd_parts = [str(bin_path), "-n", "-t", str(test_plan), "-l", str(results_path)]
    if jmeter_log_path:
        cmd_parts += ["-j", str(jmeter_log_path)]
    cmd_parts += prop_args

    return env_prefix + " ".join(shlex.quote(c) for c in cmd_parts)

# ──────────────────────────────────────────────────────────────────────────────
# Main runner
# ──────────────────────────────────────────────────────────────────────────────

def run_jmeter_remote(jm: dict, iter_idx: int, total_iters: int) -> None:
    """
    Run JMeter on the remote host and save outputs with the common filename pattern:

      jmeter_{experiment_type}_{YYYYMMDD_HHMMSS}_{iter}_{total}.jtl
      jmeter_{experiment_type}_{YYYYMMDD_HHMMSS}_{iter}_{total}.log   (JMETER CLI -j)
      jmeter_{experiment_type}_{YYYYMMDD_HHMMSS}_{iter}_{total}.stdout.log (captured stream)

    Side effects:
      - jm["__jmeter_dt__"]       : date-time string used for filenames
      - jm["__jmeter_results__"]  : remote path to .jtl
      - jm["__jmeter_jlog__"]     : remote path to JMeter's -j log
      - jm["__jmeter_stdout__"]   : remote path to streamed stdout log
      - jm["__jmeter_pid__"]      : captured remote PID (best-effort)
    """
    jmeter_host = jm.get("target_host")
    remote_dir  = jm.get("remote_dir")
    bin_path    = jm.get("bin_path")
    test_plan   = jm.get("test_plan")

    if not jmeter_host:
        raise RuntimeError("[JMeter] jmeter.target_host missing in config.")
    if not remote_dir or not bin_path or not test_plan:
        raise RuntimeError("[JMeter] bin_path / test_plan / remote_dir are required in config.")

    j_user = os.environ.get("JMETER_SSH_USER")
    j_pass = os.environ.get("JMETER_SSH_PASSWORD")
    if not j_user or not j_pass:
        raise RuntimeError("[JMeter] Missing SSH credentials. Set JMETER_SSH_USER / JMETER_SSH_PASSWORD.")

    # Naming context
    experiment_type = jm.get("experiment_type") or "experiment"
    iteration       = int(jm.get("iteration") or iter_idx or 1)
    total           = int(jm.get("total_iterations") or total_iters or 1)

    # Filenames (pattern-aligned)
    dt = _ts("%Y%m%d_%H%M%S")
    jtl_name       = _build_standard_filename("jmeter", experiment_type, dt, iteration, total, ".jtl")
    jlog_name      = _build_standard_filename("jmeter", experiment_type, dt, iteration, total, ".log")  # JMeter -j
    stdoutlog_name = _build_standard_filename("jmeter", experiment_type, dt, iteration, total, ".stdout.log")

    remote_dir = _normalize_posix(remote_dir)
    if not remote_dir.endswith('/'):
        remote_dir += '/'

    results_path   = posixpath.join(remote_dir, jtl_name)
    jlog_path      = posixpath.join(remote_dir, jlog_name)
    stdoutlog_path = posixpath.join(remote_dir, stdoutlog_name)

    # Persist context for fetch/cleanup
    jm["__jmeter_dt__"]       = dt
    jm["__jmeter_results__"]  = results_path
    jm["__jmeter_jlog__"]     = jlog_path
    jm["__jmeter_stdout__"]   = stdoutlog_path

    command = build_jmeter_command(jm, results_path, jmeter_log_path=jlog_path)

    # Capture shell PID on first line, then stream logs and tee to stdoutlog_path
    wrapped_command = (
        f'bash -lc "set -o pipefail; echo $$; '
        f'{command} 2>&1 | tee -a {shlex.quote(stdoutlog_path)}; '
        f'exit ${{PIPESTATUS[0]}}"' )

    bin_dir = posixpath.dirname(bin_path)
    chmod_cmd      = f"find {shlex.quote(bin_dir)} -maxdepth 1 -type f -exec chmod +x {{}} \\;; true"
    ensure_dir_cmd = f"mkdir -p {shlex.quote(_normalize_posix(posixpath.dirname(results_path)))}"

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

    try:
        client.connect(jmeter_host, username=j_user, password=j_pass)
        client.exec_command(ensure_dir_cmd)
        client.exec_command(chmod_cmd)

        print(f"[JMeter] Starting load (iter {iteration}/{total}) @ {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        print(f"[JMeter] Command: {command}")
        print(f"[JMeter] Results file: {results_path}")
        print(f"[JMeter] JMeter log (-j): {jlog_path}")
        print(f"[JMeter] Stdout log: {stdoutlog_path}")

        stdin, stdout, stderr = client.exec_command(wrapped_command)

        # First line should be the PID from `echo $$`
        pid_line = stdout.readline().strip()
        if pid_line.isdigit():
            print(f"[JMeter] PID: {pid_line}")
            jm["__jmeter_pid__"] = pid_line
        else:
            print(f"[JMeter] PID not captured (got: {pid_line!r})")

        # Stream JMeter output
        try:
            for line in stdout:
                if line.strip():
                    print(f"[JMeter] {line.strip()}")
        except Exception:
            # If streaming fails mid-run, keep going to capture exit status.
            pass

        exit_status = stdout.channel.recv_exit_status()
        if exit_status == 0:
            print(f"[JMeter] Completed successfully @ {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        else:
            print(f"[JMeter] FAILED with exit code {exit_status}")
            err = stderr.read().decode(errors="replace")
            if err.strip():
                print(f"[JMeter][stderr] {err}")

    except Exception as e:
        print(f"[JMeter] Error: {type(e).__name__}: {e}")
    finally:
        try:
            client.close()
        except Exception:
            pass


# ──────────────────────────────────────────────────────────────────────────────
# Fetch + cleanup
# ──────────────────────────────────────────────────────────────────────────────

def stat_isdir(attr) -> bool:
    """Return True if SFTPAttributes indicate a directory."""
    import stat as pystat
    return pystat.S_ISDIR(attr.st_mode)


def _list_remote_files(sftp: paramiko.SFTPClient, remote_dir: str) -> List[Tuple[str, int]]:
    """
    Non-recursive listing of files in a remote directory.

    Returns
    -------
    list[tuple[str, int]]
        A list of (path, size).
    """
    out: List[Tuple[str, int]] = []
    try:
        for attr in sftp.listdir_attr(remote_dir):
            p = posixpath.join(remote_dir, attr.filename)
            if not stat_isdir(attr):
                out.append((p, attr.st_size))
    except FileNotFoundError:
        pass
    return out


def _ensure_local_dir(p: str) -> None:
    """Create a local directory if it does not exist."""
    os.makedirs(p, exist_ok=True)


def _copy_and_verify(sftp: paramiko.SFTPClient, remote_path: str, local_path: str) -> bool:
    """
    Copy a file via SFTP and verify size equality.

    Returns
    -------
    bool
        True if the copy succeeded and sizes match.
    """
    try:
        remote_size = sftp.stat(remote_path).st_size
        sftp.get(remote_path, local_path)
        local_size = os.path.getsize(local_path)
        if remote_size == local_size:
            return True
        print(f"[JMeter][fetch] Size mismatch for {remote_path} (remote {remote_size} != local {local_size})")
        return False
    except Exception as e:
        print(f"[JMeter][fetch] Error copying {remote_path}: {type(e).__name__}: {e}")
        return False


def fetch_jmeter_artifacts(
    jm: dict,
    local_output_root: str,
    include_logs: bool = True,
    cleanup_remote: bool = True
) -> None:
    """
    Pull JMeter artifacts from the remote host into:
      {local_output_root}/{YYYYMMDD}/jmeter-result_{HHMMSS}/

    Files pulled:
      - The run-specific .jtl and (optionally) the per-run -j log and stdout log
      - Additionally (optional): /home/jmeter/jmeter.log if it exists (legacy/global)

    After successful copy, delete the remote originals if `cleanup_remote=True`.
    """
    jmeter_host = jm.get("target_host")
    remote_dir  = jm.get("remote_dir")

    if not jmeter_host or not remote_dir:
        print("[JMeter][fetch] Missing target_host/remote_dir; skip fetch.")
        return

    j_user = os.environ.get("JMETER_SSH_USER")
    j_pass = os.environ.get("JMETER_SSH_PASSWORD")
    if not j_user or not j_pass:
        print("[JMeter][fetch] Missing SSH creds; skip fetch.")
        return

    # Determine destination folder based on the dt used for filenames
    dt = jm.get("__jmeter_dt__") or _ts("%Y%m%d_%H%M%S")
    day_tag, time_tag = dt.split("_", 1)

    # Use the orchestrator-provided root as-is (absolute or relative)
    dest_dir = os.path.join(local_output_root, f"jmeter-result_{time_tag}")
    _ensure_local_dir(dest_dir)

    print(f"[JMeter][fetch] Downloading artifacts to: {dest_dir}")

    client: paramiko.SSHClient | None = None
    sftp: paramiko.SFTPClient | None = None
    try:
        client, sftp = _sftp(jmeter_host, j_user, j_pass)

        to_fetch: list[str] = []

        # 1) Always fetch the run-specific .jtl and (optionally) logs from remote_dir
        for key in ("__jmeter_results__", "__jmeter_jlog__", "__jmeter_stdout__"):
            pth = jm.get(key)
            if pth:
                if key == "__jmeter_results__" or include_logs:
                    to_fetch.append(pth)

        # 2) Optionally fetch the big /home/jmeter/jmeter.log (if exists; legacy)
        if include_logs:
            home_jm_log = "/home/jmeter/jmeter.log"
            try:
                sftp.stat(home_jm_log)  # exists?
                to_fetch.append(home_jm_log)
            except FileNotFoundError:
                pass

        # Deduplicate while keeping order
        to_fetch = list(dict.fromkeys(to_fetch))

        # Copy each and delete on success
        for rpath in to_fetch:
            fname = posixpath.basename(rpath)
            lpath = os.path.join(dest_dir, fname)
            ok = _copy_and_verify(sftp, rpath, lpath)
            if ok and cleanup_remote:
                try:
                    sftp.remove(rpath)
                    print(f"[JMeter][fetch] Removed remote: {rpath}")
                except Exception as e:
                    print(f"[JMeter][fetch] Could not remove {rpath}: {type(e).__name__}: {e}")
            elif not ok:
                print(f"[JMeter][fetch] Kept remote (copy failed): {rpath}")

        print(f"[JMeter][fetch] Done. Files in: {dest_dir}")

    except Exception as e:
        print(f"[JMeter][fetch] Error: {type(e).__name__}: {e}")
    finally:
        try:
            if sftp:
                sftp.close()
        except Exception:
            pass
        try:
            if client:
                client.close()
        except Exception:
            pass


# ──────────────────────────────────────────────────────────────────────────────
# Shutdown / Stop helpers
# ──────────────────────────────────────────────────────────────────────────────

def shutdown_jmeter_remote(jm: dict) -> None:
    """
    Attempt a graceful JMeter shutdown using bundled scripts (best-effort).
    Requires 'target_host' and 'bin_path' in `jm` and JMETER_SSH_* env vars.
    """
    host     = jm.get("target_host")
    bin_path = jm.get("bin_path")
    if not host or not bin_path:
        print("[JMeter] Missing host/bin_path for graceful shutdown.")
        return

    j_user = os.environ.get("JMETER_SSH_USER")
    j_pass = os.environ.get("JMETER_SSH_PASSWORD")
    if not j_user or not j_pass:
        print("[JMeter] Missing JMETER_SSH_USER/JMETER_SSH_PASSWORD for shutdown.")
        return

    bin_dir = posixpath.dirname(bin_path)
    client: paramiko.SSHClient | None = None
    try:
        client = _connect(host, j_user, j_pass)
        client.exec_command(f"bash -lc '{shlex.quote(posixpath.join(bin_dir, 'shutdown.sh'))} || true'")
        time.sleep(2)
        client.exec_command(f"bash -lc '{shlex.quote(posixpath.join(bin_dir, 'stoptest.sh'))} || true'")
        print("[JMeter] Requested graceful shutdown via scripts.")
    except Exception as e:
        print(f"[JMeter] Graceful shutdown error: {type(e).__name__}: {e}")
    finally:
        if client:
            try:
                client.close()
            except Exception:
                pass


def stop_jmeter_remote(jm: dict) -> None:
    """
    Send TERM to the recorded JMeter PID (best-effort).
    Requires '__jmeter_pid__', 'target_host', and JMETER_SSH_* env vars.
    """
    pid = jm.get("__jmeter_pid__")
    jmeter_host = jm.get("target_host")
    if not pid or not jmeter_host:
        print("[JMeter] No PID or host recorded, nothing to stop.")
        return

    j_user = os.environ.get("JMETER_SSH_USER")
    j_pass = os.environ.get("JMETER_SSH_PASSWORD")
    if not j_user or not j_pass:
        raise RuntimeError("[JMeter] Missing SSH credentials to stop JMeter.")

    client: paramiko.SSHClient | None = None
    try:
        client = _connect(jmeter_host, j_user, j_pass)
        _, stdout, stderr = client.exec_command(f"kill -TERM {pid} || true")
        err = stderr.read().decode(errors="replace").strip()
        if err:
            print(f"[JMeter][stderr] {err}")
        else:
            print(f"[JMeter] Sent TERM to PID {pid}")
    finally:
        if client:
            client.close()


def force_kill_jmeter_remote(jm: dict) -> None:
    """
    Send KILL to the recorded JMeter PID (best-effort).
    Requires '__jmeter_pid__', 'target_host', and JMETER_SSH_* env vars.
    """
    pid = jm.get("__jmeter_pid__")
    jmeter_host = jm.get("target_host")
    if not pid or not jmeter_host:
        print("[JMeter] No PID or host recorded, nothing to kill.")
        return

    j_user = os.environ.get("JMETER_SSH_USER")
    j_pass = os.environ.get("JMETER_SSH_PASSWORD")
    if not j_user or not j_pass:
        raise RuntimeError("[JMeter] Missing SSH credentials to kill JMeter.")

    client: paramiko.SSHClient | None = None
    try:
        client = _connect(jmeter_host, j_user, j_pass)
        _, stdout, stderr = client.exec_command(f"kill -KILL {pid} || true")
        err = stderr.read().decode(errors="replace").strip()
        if err:
            print(f"[JMeter][stderr] {err}")
        else:
            print(f"[JMeter] Sent KILL to PID {pid}")
    finally:
        if client:
            client.close()
