# hooks.py
"""
Helpers to start/stop TeaStore in two modes:
- Docker Compose
- Individual Tomcat services (optionally instrumented)

Notes
-----
- Public function signatures are unchanged.
- Prints and general behavior remain the same.
"""

# ──────────────────────────────────────────────────────────────────────────────
# Imports & Constants
# ──────────────────────────────────────────────────────────────────────────────

from __future__ import annotations

import os
import shutil
import subprocess
from datetime import datetime
from typing import Dict
import time

import paramiko


# ──────────────────────────────────────────────────────────────────────────────
# Internal helpers (no API changes)
# ──────────────────────────────────────────────────────────────────────────────

def _connect(hostname: str, username: str, password: str, port: int = 22) -> paramiko.SSHClient:
    """
    Open an SSH connection to a host with password auth.

    Parameters
    ----------
    hostname : str
    username : str
    password : str
    port : int, optional
        SSH port (default 22).

    Returns
    -------
    paramiko.SSHClient
    """
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(hostname, port=port, username=username, password=password)
    return client


def _cleanup_powerapi_reporting_dir(client: paramiko.SSHClient) -> None:
    """Remove stale PowerAPI sensor output files before launching the stack."""
    cleanup_cmd = "rm -rf /tmp/powerapi-sensor-reporting/*"
    _, stdout, stderr = client.exec_command(cleanup_cmd)
    exit_status = stdout.channel.recv_exit_status()
    if exit_status != 0:
        print("[!] Failed to clean PowerAPI reporting directory.")
        print(stderr.read().decode())
        raise RuntimeError("PowerAPI reporting cleanup failed.")


def _download_powerapi_reporting_files(client: paramiko.SSHClient, output_dir: str) -> None:
    """Download all PowerAPI sensor reporting files to local output directory."""
    local_target_dir = os.path.join(output_dir, "powerapi-sensor-reporting")
    os.makedirs(local_target_dir, exist_ok=True)

    # List all files in remote directory
    list_cmd = "find /tmp/powerapi-sensor-reporting -type f"
    _, stdout, stderr = client.exec_command(list_cmd)
    remote_files = stdout.read().decode().strip().split('\n')

    if not remote_files or remote_files == ['']:
        print("[!] No PowerAPI reporting files found to download.")
        return

    sftp = client.open_sftp()
    try:
        downloaded_count = 0
        for remote_file in remote_files:
            remote_file = remote_file.strip()
            if not remote_file:
                continue

            filename = os.path.basename(remote_file)
            local_file = os.path.join(local_target_dir, filename)

            try:
                sftp.get(remote_file, local_file)
                downloaded_count += 1
            except Exception as e:
                print(f"[!] Failed to download {remote_file}: {e}")

        print(f"[✓] Downloaded {downloaded_count} PowerAPI reporting file(s) to {local_target_dir}")
    finally:
        sftp.close()


def _process_powerapi_reporting_files(output_dir: str) -> None:
    """Run SmartWatts formula over downloaded PowerAPI CSV reports."""
    if shutil.which("podman") is None:
        raise RuntimeError("podman executable not found in PATH.")

    base_dir = os.path.abspath(output_dir)
    reports_dir = os.path.join(base_dir, "powerapi-sensor-reporting")
    results_dir = os.path.join(base_dir, "smartwattsresults")
    os.makedirs(results_dir, exist_ok=True)

    required_files = ["core.csv", "msr.csv", "rapl.csv"]
    missing_files = [name for name in required_files if not os.path.isfile(os.path.join(reports_dir, name))]
    if missing_files:
        missing = ", ".join(missing_files)
        raise RuntimeError(f"Missing required PowerAPI report files: {missing}")

    cmd = [
        "podman", "run", "--rm",
        "-v", f"{base_dir}:/work",
        "powerapi/smartwatts-formula",
        "--verbose",
        "--input", "csv",
        "--files", "/work/powerapi-sensor-reporting/core.csv,/work/powerapi-sensor-reporting/msr.csv,/work/powerapi-sensor-reporting/rapl.csv",
        "--model", "HWPCReport",
        "--output", "csv",
        "--directory", "/work/smartwattsresults",
        "--cpu-base-freq", "2000",
        "--cpu-error-threshold", "2.0",
        "--disable-dram-formula",
        "--sensor-reports-frequency", "1000",
    ]

    print("[~] Processing PowerAPI reports with SmartWatts ...")
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print("[!] SmartWatts processing failed.")
        if result.stdout:
            print(result.stdout)
        if result.stderr:
            print(result.stderr)
        raise RuntimeError("SmartWatts processing failed.")

    if result.stdout:
        print(result.stdout.strip())
    print(f"[✓] SmartWatts results written to {results_dir}")


# ──────────────────────────────────────────────────────────────────────────────
# Docker helpers
# ──────────────────────────────────────────────────────────────────────────────

def start_docker(hostname: str, username: str, password: str, config: dict, output_dir: str) -> None:
    """
    Start using Docker Compose.

    Reads the command from:
        config['experiment']['remote_docker_start']

    Raises
    ------
    RuntimeError
        If the remote command returns a non-zero exit code.
    """
    _ = output_dir  # kept for hook signature compatibility
    print("[~] Starting Docker ...")
    compose_up_cmds = config['experiment']['remote_docker_start']

    if isinstance(compose_up_cmds, str):
        compose_up_cmds = [compose_up_cmds]

    client = _connect(hostname, username, password)

    try:
        if config.get("experiment", {}).get("type", "").startswith("spring_docker_powerapi"):
            print("[~] Cleaning PowerAPI reporting directory ...")
            _cleanup_powerapi_reporting_dir(client)

        if config["experiment"]["type"].startswith("spring_docker_joularjx"):
            result_dir = config["experiment"]["joularjx_result_dir"]
            client.exec_command(f"mkdir -p {result_dir}")
            client.exec_command(f"rm -rf {result_dir}/*")
            if config["experiment"]["type"].endswith(("_rs2", "_rs3")):
                result_dir_2 = f"{result_dir}-2"
                client.exec_command(f"mkdir -p {result_dir_2}")
                client.exec_command(f"rm -rf {result_dir_2}/*")

        for compose_up_cmd in compose_up_cmds:
            _, stdout, stderr = client.exec_command(compose_up_cmd)
            exit_status = stdout.channel.recv_exit_status()
            if exit_status != 0:
                print("[!] Failed to start via Docker.")
                print(stderr.read().decode())
                raise RuntimeError("Docker startup failed.")
            
        # Verify NUMA binding for rs2 experiments
        if config["experiment"]["type"].endswith("_rs2"):
            print("[~] Verifying NUMA binding ...")

            inspect_cmds = {
                "spring-rest-energy-test": "CPUs=0-19,40-59 MEMs=0,1",
                "spring-rest-energy-test-2": "CPUs=20-39,60-79 MEMs=2,3",
            }

            for container, expected in inspect_cmds.items():
                cmd = (
                    f"docker inspect {container} "
                    "--format 'CPUs={{.HostConfig.CpusetCpus}} "
                    "MEMs={{.HostConfig.CpusetMems}}'"
                )
                _, stdout, stderr = client.exec_command(cmd)
                result = stdout.read().decode().strip()

                if result != expected:
                    print(f"[!] NUMA binding mismatch for {container}")
                    print(f"    Expected: {expected}")
                    print(f"    Actual:   {result}")
                    raise RuntimeError("NUMA binding verification failed.")

                print(f"    ↳ {container}: {result}")

        # tbd: fix name logic
        # Fetch PID (best-effort)
        time.sleep(2)
        containers = ["spring-rest-energy-test"]

        if config["experiment"]["type"].endswith(("_rs2", "_rs3")):
            containers.append("spring-rest-energy-test-2")

        pids: Dict[str, str] = {}

        for container in containers:
            get_pid_cmd = (
                f"docker inspect {container} | "
                "jq -r '.[0].State.Pid'"
            )

            _, stdout, stderr = client.exec_command(get_pid_cmd)
            pid = stdout.read().decode().strip()
            err_output = stderr.read().decode().strip()

            if pid.isdigit():
                print(f"    ↳ {container}: PID {pid}")
                pids[container] = pid
            else:
                print(f"[!] Could not retrieve PID for {container}. stderr: {err_output}")

        # Persist for use in the runner (unchanged behavior)
        config['__springpids__'] = pids

        print("[✓] Docker started successfully.")
    finally:
        client.close()


def stop_docker(hostname: str, username: str, password: str, config: dict, output_dir: str) -> None:
    """
    Stop using Docker Compose.

    Reads the command from:
        config['experiment']['remote_docker_stop']
    """
    print("[~] Stopping Docker ...")
    compose_stop_cmds = config['experiment']['remote_docker_stop']
    compose_logs_cmds = config['experiment']['remote_docker_logs']
    if isinstance(compose_stop_cmds, str):
        compose_stop_cmds = [compose_stop_cmds]
    if isinstance(compose_logs_cmds, str):
        compose_logs_cmds = [compose_logs_cmds]

    client = _connect(hostname, username, password)
    try:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_filename = f"docker_compose_logs_{timestamp}.txt"

        os.makedirs(output_dir, exist_ok=True)
        output_path = os.path.join(output_dir, output_filename)


        with open(output_path, "w", encoding="utf-8") as f:
            for i, compose_logs_cmd in enumerate(compose_logs_cmds, start=1):
                _, stdout, stderr = client.exec_command(compose_logs_cmd)
                output_content = stdout.read().decode()

                f.write(f"\n===== Docker Compose Logs {i} =====\n\n")
                f.write(output_content)
                f.write("\n")

        for compose_stop_cmd in compose_stop_cmds:
            _, stdout, stderr = client.exec_command(compose_stop_cmd)
            exit_status = stdout.channel.recv_exit_status()
            if exit_status != 0:
                print("[!] Failed to stop via Docker.")
                print(stderr.read().decode())
            else:
                print("[✓] Docker stopped.")

        # Download PowerAPI reporting files if this is a PowerAPI experiment
        experiment_type = config.get("experiment", {}).get("type", "")
        if "spring_docker_powerapi" in experiment_type:
            print("[~] Downloading PowerAPI reporting files ...")
            _download_powerapi_reporting_files(client, output_dir)
            _process_powerapi_reporting_files(output_dir)
    finally:
        client.close()


# ──────────────────────────────────────────────────────────────────────────────
# VM helpers  (host = hypervisor for tools, guest = VM for the application)
# ──────────────────────────────────────────────────────────────────────────────

def _get_vm_guest_connection(config: dict) -> tuple:
    """
    Return (guest_host, guest_port, guest_user, guest_password) from config + env.

    Raises
    ------
    RuntimeError
        If required values are missing.
    """
    exp = config.get("experiment", {})
    guest_host = exp.get("vm_guest_host")
    guest_port = int(exp.get("vm_guest_ssh_port", 2222))
    guest_user = os.environ.get("VM_SSH_USER")
    guest_pass = os.environ.get("VM_SSH_PASSWORD")

    if not guest_host:
        raise RuntimeError("Missing experiment.vm_guest_host in config.")
    if not guest_user or not guest_pass:
        raise RuntimeError(
            "Missing VM SSH credentials. Please set VM_SSH_USER and VM_SSH_PASSWORD as environment variables."
        )

    return guest_host, guest_port, guest_user, guest_pass


def start_vm_docker(hostname: str, username: str, password: str, config: dict, output_dir: str) -> None:
    """
    Start experiment in VM setup:
      1. Start the measurement tool on the host / hypervisor (if configured).
      2. Start the application via Docker Compose on the VM guest.

    Parameters
    ----------
    hostname : str
        Host / hypervisor address (from config['experiment']['target_host']).
    username, password : str
        SUT SSH credentials (for the host / hypervisor).
    config : dict
        Full experiment configuration.
    output_dir : str
        Local output directory for the current experiment run.
    """
    exp = config.get("experiment", {})
    experiment_type = exp.get("type", "")

    # --- 1) Start tool on the host / hypervisor ---
    host_tool_start_cmd = (exp.get("host_tool_start") or "").strip()
    if host_tool_start_cmd:
        print(f"[VM] Starting tool on host ({hostname}) ...")
        host_client = _connect(hostname, username, password)
        try:
            # Clean PowerAPI reporting dir before launch
            if experiment_type == "spring_vm_powerapi":
                print("[VM] Cleaning PowerAPI reporting directory on host ...")
                _cleanup_powerapi_reporting_dir(host_client)

            _, stdout, stderr = host_client.exec_command(host_tool_start_cmd)
            exit_status = stdout.channel.recv_exit_status()
            if exit_status != 0:
                err = stderr.read().decode().strip()
                out = stdout.read().decode().strip()
                print(f"[!] Failed to start tool on host (exit {exit_status}).")
                print(f"[!] Command: {host_tool_start_cmd}")
                if out:
                    print(f"[!] stdout: {out}")
                if err:
                    print(f"[!] stderr: {err}")
                raise RuntimeError(
                    f"Host tool startup failed (exit {exit_status}).\n"
                    f"Command: {host_tool_start_cmd}\n"
                    f"stdout: {out}\n"
                    f"stderr: {err}"
                )
            print("[VM] ✓ Tool started on host.")
        finally:
            host_client.close()

    # wait for directory to become available
    import time
    time.sleep(10)

    # --- 2) Start application on the VM guest ---
    guest_host, guest_port, guest_user, guest_pass = _get_vm_guest_connection(config)
    compose_up_cmd = exp.get("remote_docker_start", "")
    if not compose_up_cmd:
        raise RuntimeError("Missing experiment.remote_docker_start in config.")

    print(f"[VM] Starting application on VM guest ({guest_host}:{guest_port}) ...")
    guest_client = _connect(guest_host, guest_user, guest_pass, port=guest_port)
    try:
        _, stdout, stderr = guest_client.exec_command(compose_up_cmd)
        exit_status = stdout.channel.recv_exit_status()
        if exit_status != 0:
            err = stderr.read().decode()
            print(f"[!] Failed to start Docker on VM guest: {err}")
            raise RuntimeError("VM guest Docker startup failed.")

        # Fetch application PID inside the guest (best-effort)
        #get_pid_cmd = "docker inspect spring-rest-service-spring-rest-service-1 | jq -r '.[0].State.Pid'"
        container_name = exp.get(
            "vm_container_name",
            "spring-rest-service-spring-rest-service-1",
        )
        print(f"[DEBUG] exp = {exp}")
        print(f"[DEBUG] container_name = {container_name}")

        get_pid_cmd = """
            docker inspect -f '{{.State.Pid}}' \
            $(docker ps --format '{{.Names}}' | grep spring-rest-energy-test | head -n1)
        """
        
        print(f"[DEBUG] get_pid_cmd = {get_pid_cmd}")
        _, stdout, stderr = guest_client.exec_command(get_pid_cmd)
        pid = stdout.read().decode().strip()
        print(f"[DEBUG] pid = '{pid}'")
        pids: Dict[str, str] = {}
        if pid.isdigit():
            print(f"[VM]    ↳ Guest PID: {pid}")
            pids['spring-rest-service'] = pid
        else:
            print(f"[VM] [!] Could not retrieve PID on VM guest.")

        config['__springpids__'] = pids
        print("[VM] ✓ Application started on VM guest.")
    finally:
        guest_client.close()


def stop_vm_docker(hostname: str, username: str, password: str, config: dict, output_dir: str) -> None:
    """
    Stop experiment in VM setup:
      1. Stop the application on the VM guest and download logs.
      2. Stop the measurement tool on the host / hypervisor and download artifacts.

    Parameters
    ----------
    hostname : str
        Host / hypervisor address.
    username, password : str
        SUT SSH credentials (for the host / hypervisor).
    config : dict
        Full experiment configuration.
    output_dir : str
        Local output directory for the current experiment run.
    """
    exp = config.get("experiment", {})
    experiment_type = exp.get("type", "")

    # --- 1) Stop application on VM guest and fetch logs ---
    guest_host, guest_port, guest_user, guest_pass = _get_vm_guest_connection(config)
    compose_stop_cmd = exp.get("remote_docker_stop", "")
    compose_logs_cmd = exp.get("remote_docker_logs", "")

    print(f"[VM] Stopping application on VM guest ({guest_host}:{guest_port}) ...")
    guest_client = _connect(guest_host, guest_user, guest_pass, port=guest_port)
    try:
        # Download docker-compose logs
        if compose_logs_cmd:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            output_filename = f"docker_compose_logs_{timestamp}.txt"
            os.makedirs(output_dir, exist_ok=True)
            output_path = os.path.join(output_dir, output_filename)

            _, stdout, stderr = guest_client.exec_command(compose_logs_cmd)
            output_content = stdout.read().decode().strip()
            with open(output_path, 'w', encoding='utf-8') as f:
                f.write(output_content)

        # Stop docker-compose
        if compose_stop_cmd:
            _, stdout, stderr = guest_client.exec_command(compose_stop_cmd)
            exit_status = stdout.channel.recv_exit_status()
            if exit_status != 0:
                print("[!] Failed to stop Docker on VM guest.")
                print(stderr.read().decode())
            else:
                print("[VM] ✓ Application stopped on VM guest.")
    finally:
        guest_client.close()

    # --- 2) Stop tool on host / hypervisor and collect artifacts ---
    host_tool_stop_cmd = (exp.get("host_tool_stop") or "").strip()
    host_tool_logs_cmd = (exp.get("host_tool_logs") or "").strip()

    if host_tool_stop_cmd or host_tool_logs_cmd:
        print(f"[VM] Stopping tool on host ({hostname}) ...")
        host_client = _connect(hostname, username, password)
        try:
            # Collect tool logs before stopping
            if host_tool_logs_cmd:
                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                log_filename = f"host_tool_logs_{timestamp}.txt"
                os.makedirs(output_dir, exist_ok=True)
                log_path = os.path.join(output_dir, log_filename)

                _, stdout, stderr = host_client.exec_command(host_tool_logs_cmd)
                log_content = stdout.read().decode().strip()
                with open(log_path, 'w', encoding='utf-8') as f:
                    f.write(log_content)

            # Stop the tool
            if host_tool_stop_cmd:
                _, stdout, stderr = host_client.exec_command(host_tool_stop_cmd)
                exit_status = stdout.channel.recv_exit_status()
                if exit_status != 0:
                    print("[!] Failed to stop tool on host.")
                    print(stderr.read().decode())
                else:
                    print("[VM] ✓ Tool stopped on host.")

            # Download PowerAPI artifacts from the host
            if experiment_type == "spring_vm_powerapi":
                print("[VM] Downloading PowerAPI reporting files from host ...")
                _download_powerapi_reporting_files(host_client, output_dir)
                _process_powerapi_reporting_files(output_dir)
        finally:
            host_client.close()
