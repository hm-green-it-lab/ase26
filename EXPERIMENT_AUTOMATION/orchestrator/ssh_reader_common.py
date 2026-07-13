# ssh_reader_common.py
"""Shared SSH/SFTP helpers for remote reader orchestration."""

from __future__ import annotations

import json
import os
import time
from typing import Tuple

import paramiko


def log_experiment(metadata: dict, log_path: str) -> None:
    """Append a single-line JSON record to the given log file."""
    os.makedirs(os.path.dirname(log_path), exist_ok=True)
    with open(log_path, "a", encoding="utf-8") as log_file:
        log_file.write(json.dumps(metadata) + "\n")


def connect_ssh(hostname: str, username: str, password: str, port: int | None = None,) -> paramiko.SSHClient:
    """Create and connect an SSH client with auto-accept host keys."""
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    kwargs = {
        "hostname": hostname,
        "username": username,
        "password": password,
    }

    if port is not None:
        kwargs["port"] = port

    ssh.connect(**kwargs)
    return ssh


def start_remote_and_get_pid(
    ssh: paramiko.SSHClient,
    remote_cmd: str,
    timeout_seconds: float = 5.0,
) -> Tuple[str, str]:
    """Run a remote command and read the first non-empty stdout line as PID."""
    _, stdout, stderr = ssh.exec_command(remote_cmd, get_pty=True)

    pid = ""
    start_wait = time.time()
    while True:
        if stdout.channel.recv_ready():
            pid_line = stdout.readline().strip()
            if pid_line:
                pid = pid_line
                break
        if time.time() - start_wait > timeout_seconds:
            break
        time.sleep(0.1)

    err_output = stderr.read().decode(errors="replace").strip()
    return pid, err_output


def stop_remote_pid_best_effort(ssh: paramiko.SSHClient, pid: str) -> bool:
    """Return True if PID was running and a kill command was sent."""
    check_cmd = f"ps -p {pid} > /dev/null && echo RUNNING || echo GONE"
    _, stdout, _ = ssh.exec_command(check_cmd)
    state = stdout.read().decode().strip()

    if state == "RUNNING":
        ssh.exec_command(f"sudo kill {pid}")
        return True

    return False


def download_and_cleanup_remote_file(
    ssh: paramiko.SSHClient,
    remote_output_file: str,
    local_output_file: str,
    remove_remote: bool = True,
) -> None:
    """Download a remote file to local path and optionally delete the remote copy."""
    sftp = None
    try:
        sftp = ssh.open_sftp()
        sftp.stat(remote_output_file)

        os.makedirs(os.path.dirname(local_output_file), exist_ok=True)
        sftp.get(remote_output_file, local_output_file)

        if remove_remote:
            sftp.remove(remote_output_file)
    finally:
        if sftp is not None:
            sftp.close()

def get_vm_pid(
    hostname: str,
    username: str,
    password: str,
) -> str:
    ssh = connect_ssh(
        hostname=hostname,
        username=username,
        password=password,
    )

    try:
        stdin, stdout, stderr = ssh.exec_command(
            "pgrep -f 'guest=vm1' | head -n1"
        )

        pid = stdout.read().decode().strip()

        if not pid:
            raise RuntimeError("Could not determine VM PID")

        return pid

    finally:
        ssh.close()