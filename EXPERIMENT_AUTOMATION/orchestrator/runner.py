# runner.py
"""
Top-level experiment runner.

Responsibilities
---------------
- Validate SSH credentials via env (SUT_SSH_USER / SUT_SSH_PASSWORD).
- Optionally run pre/post experiment hooks.
- Check remote clock drift (best-effort guard).
- Start measurement readers (Rittal local, Powercap remote, ProcFS remote).
- Optionally start JMeter in parallel (unless baseline/no-load).
- Ensure orderly shutdown of JMeter, with TERM→KILL fallback.
- Fetch JMeter artifacts to the local output folder.

Notes
-----
- Public function signature and overall behavior are preserved.
- Prints are kept and only minimally reworded for consistency.
"""

from __future__ import annotations

import datetime
import os
import threading
import time
from typing import Callable, Optional

from orchestrator.ssh_powercap_reader import run_remote_powercap
from orchestrator.ssh_reader_common import get_vm_pid
from orchestrator.ssh_procfs_reader import run_remote_procfs
from orchestrator.local_rittal_reader import run_local_rittal_reader
from helper.clock_drift import check_remote_clock_drift
from helper.jmeter import (
    run_jmeter_remote,
    shutdown_jmeter_remote,
    stop_jmeter_remote,
    force_kill_jmeter_remote,
    fetch_jmeter_artifacts,
)

from helper.joularjx import fetch_joularjx_artifacts

from orchestrator.local_http_logger import run_local_http_logger


# ──────────────────────────────────────────────────────────────────────────────
# Public API
# ──────────────────────────────────────────────────────────────────────────────

def run_experiment(
    config: dict,
    experiment_type: str,
    before_experiment_hook: Optional[Callable] = None,
    after_experiment_hook: Optional[Callable] = None,
) -> None:
    """
    Execute one or more experiment iterations as configured.

    Parameters
    ----------
    config : dict
        Full configuration dictionary. Expected keys:
          config['experiment'] : {
            'target_host'            : str,
            'remote_dir'             : str,
            'powercap_jar_filename'  : str,
            'procfs_jar_filename'    : str,
            'rittal_jar_path'        : str,
            'duration'               : int,
            'iterations'             : int (optional, default 1),
            'wait_between_runs'      : int (optional, seconds, default 0),
            'local_output_directory' : str,
            'interval'               : str (optional; CRON-like, default */1 * * * * ?),
          }
          config['jmeter'] : {
            'enabled'             : bool,
            'summary_wait_secs'   : int (optional, default 5),
            'download_logs'       : bool (optional, default True),
            # plus standard JMeter fields used by helper.jmeter
          }

    experiment_type : str
        Label/tag for this experiment (used in filenames, logs).
    before_experiment_hook : callable(hostname, username, password, config), optional
        Called right before each iteration starts (e.g., to start services).
    after_experiment_hook : callable(hostname, username, password, config), optional
        Called after readers and JMeter have been stopped (e.g., to collect logs).

    Behavior
    --------
    - Runs `iterations` times. Each iteration:
        1) pre-hook (optional)
        2) clock drift check
        3) start JMeter (optional, unless baseline/no-load)
        4) start readers (Rittal always; Powercap & ProcFS unless baseline)
        5) wait for readers
        6) graceful JMeter shutdown and fetch artifacts
        7) post-hook (optional)
        8) wait_between_runs (optional)
    """
    exp = config['experiment']

    hostname = exp['target_host']
    username = os.environ.get("SUT_SSH_USER")
    password = os.environ.get("SUT_SSH_PASSWORD")

    hostname_vm = exp['target_host']
    username_vm = os.environ.get("SUT_SSH_USER")
    password_vm = os.environ.get("SUT_SSH_PASSWORD")

    if not username or not password:
        raise RuntimeError(
            "Missing SSH credentials. Please set SUT_SSH_USER and SUT_SSH_PASSWORD as environment variables."
        )

    remote_dir = exp['remote_dir']

    vm_remote_dir = None
    if exp.get("vm_guest_host"):
        vm_remote_dir = exp.get("vm_remote_dir")

    powercap_jar = exp['powercap_jar_filename']

    procfs_jar = exp['procfs_jar_filename']
    rittal_jar_path = exp['rittal_jar_path']
    duration = int(exp['duration'])
    iterations = int(exp.get('iterations', 1))
    wait_between = int(exp.get('wait_between_runs', 0))
    # Create timestamped output directory
    date_timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")

    output_dir = os.path.abspath(os.path.join(
        os.path.dirname(__file__),
        '..',
        exp['local_output_directory'],
        date_timestamp + '_' + experiment_type
    ))

    interval = exp.get("interval", "*/1 * * * * ?")

    # --- JMeter config (optional) ---
    jm = (config.get("jmeter") or {})
    jmeter_enabled = bool(jm.get("enabled", False))
    jm_summary_wait = int(jm.get("summary_wait_secs", 5))  # allow summariser flush

    # Baseline/no-load experiment types that must never start JMeter
    NO_LOAD_TYPES = {'baseline_idle_no_tools'}

    # Experiment types that must start JMeter but not any measurement tools
    NO_TOOL_TYPES = {'spring_docker_none'}

    for i in range(1, iterations + 1):
        # ── Iteration header ───────────────────────────────────────────────────
        start_time = datetime.datetime.now()
        end_time = start_time + datetime.timedelta(seconds=duration)
        print(
            f"\n[*] Starting {experiment_type} iteration {i} of {iterations} - results logged in {output_dir}..."
            f"\n    → Start: {start_time.strftime('%Y-%m-%d %H:%M:%S')}"
            f"\n    → Estimated End: {end_time.strftime('%Y-%m-%d %H:%M:%S')} (Duration: {duration}s + Initialization Phase)\n"
        )

        # ── Pre-hook ───────────────────────────────────────────────────────────
        if before_experiment_hook:
            print(f"[~] Running pre-experiment hook for {experiment_type}...")
            before_experiment_hook(hostname, username, password, config, output_dir)

        # ── Clock drift guard ──────────────────────────────────────────────────
        print(f"[~] Check remote clock drift (host) ...")
        check_remote_clock_drift(hostname, username, password)

        # For VM experiments, also check clock drift on the VM guest
        vm_guest_host = exp.get("vm_guest_host")

        vm_guest_port = None
        vm_user = None
        vm_pass = None
        vm_pid = None

        if vm_guest_host:
            vm_guest_port = int(exp.get("vm_guest_ssh_port", 2222))
            vm_user = os.environ.get("VM_SSH_USER")
            vm_pass = os.environ.get("VM_SSH_PASSWORD")
            vm_pid = get_vm_pid(
                hostname,
                username,
                password,
            )
            if vm_user and vm_pass:
                print(f"[~] Check remote clock drift (VM guest {vm_guest_host}:{vm_guest_port}) ...")
                check_remote_clock_drift(vm_guest_host, vm_user, vm_pass, port=vm_guest_port)

        # ── Canonical start-time tag ───────────────────────────────────────────
        if "time_tag" not in exp and "__time_tag__" not in config:
            config["__time_tag__"] = datetime.datetime.now().strftime("%H%M%S")

        # ── JMeter (optional) ─────────────────────────────────────────────────
        jmeter_thread: Optional[threading.Thread] = None
        jmeter_config = None

        if jmeter_enabled and experiment_type not in NO_LOAD_TYPES:
            jmeter_config = dict(jm)  # shallow copy
            # used internally for timestamped filenames
            jmeter_config["experiment_type"] = f"{experiment_type}"      # ← use the key expected by helper.jmeter
            jmeter_config["iteration"] = i
            jmeter_config["total_iterations"] = iterations
            jmeter_thread = threading.Thread(
                target=run_jmeter_remote,
                kwargs={"jm": jmeter_config, "iter_idx": i, "total_iters": iterations},
                daemon=False,
            )
            jmeter_thread.start()

        # ── Measurement readers in parallel ───────────────────────────────────
        # Rittal: always on (local)
        rittal_thread = threading.Thread(
            target=run_local_rittal_reader,
            kwargs={
                "jar_path": rittal_jar_path,
                "duration": duration,
                "experiment_type": experiment_type,
                "iteration": i,
                "total_iterations": iterations,
                "output_dir": output_dir,
                "interval": interval,
                "rittal_snmp_address": str(exp.get("rittal_snmp_address", "")),
                "rittal_snmp_community": str(exp.get("rittal_snmp_community", "")),
                "rittal_snmp_oids": str(exp.get("rittal_snmp_oids", "")),
            },
        )

        # Powercap + ProcFS: skip for pure baseline
        powercap_thread: Optional[threading.Thread] = None
        procfs_thread: Optional[threading.Thread] = None
        http_reader_thread: Optional[threading.Thread] = None

        http_logger_jar_path = exp.get('http_logger_jar_path')

        if experiment_type not in NO_LOAD_TYPES and experiment_type not in NO_TOOL_TYPES:

            if http_logger_jar_path:
                http_reader_thread = threading.Thread(
                    target=run_local_http_logger,
                    kwargs={
                        "jar_path": http_logger_jar_path,
                        "duration": duration,
                        "experiment_type": experiment_type,
                        "iteration": i,
                        "total_iterations": iterations,
                        "http_logger_url": exp.get('http_logger_url'),
                        "output_dir": output_dir,
                        "interval": interval,
                    },
                )

            powercap_thread = threading.Thread(
                target=run_remote_powercap,
                kwargs={
                    "hostname": hostname,
                    "username": username,
                    "password": password,
                    "jar_filename": powercap_jar,
                    "remote_dir": remote_dir,
                    "duration": duration,
                    "experiment_type": experiment_type,
                    "iteration": i,
                    "total_iterations": iterations,
                    "output_dir": output_dir,
                    "interval": interval,
                },
            )

            procfs_thread = threading.Thread(
                target=run_remote_procfs,
                kwargs={
                    "hostname": hostname,
                    "username": username,
                    "password": password,
                    "remote_dir": remote_dir,
                    "procfs_jar_filename": procfs_jar,
                    # For VM experiments, guest PIDs are not visible in the host's
                    # /proc, so we pass an empty list (system-level metrics only).
                    "pids": list(config.get("__springpids__", {}).values()),
                    "experiment_type": experiment_type,
                    "iteration": i,
                    "total_iterations": iterations,
                    "duration": duration,
                    "interval": interval,
                    "output_dir": output_dir,

                    "vm_mode": bool(vm_guest_host),

                    "hostname_vm": vm_guest_host,
                    "username_vm": vm_user if vm_guest_host else None,
                    "password_vm": vm_pass if vm_guest_host else None,
                    "port_vm": vm_guest_port if vm_guest_host else None,
                    "vm_pids": [str(vm_pid)] if vm_pid else [],
                    "remote_dir_vm": vm_remote_dir,
                },
            )

        # ── Start readers ─────────────────────────────────────────────────────
        rittal_thread.start()
        if powercap_thread:
            powercap_thread.start()
        if procfs_thread:
            procfs_thread.start()
        if http_reader_thread:
            http_reader_thread.start()

        # ── Wait for readers ──────────────────────────────────────────────────
        rittal_thread.join()
        if powercap_thread:
            powercap_thread.join()
        if procfs_thread:
            procfs_thread.join()
        if http_reader_thread:
            http_reader_thread.join()

        # ── JMeter shutdown / fetch ───────────────────────────────────────────
        if jmeter_enabled and jmeter_thread and jmeter_config:
            # 1) graceful stop (lets summariser print final lines)
            shutdown_jmeter_remote(jmeter_config)

            # 2) give it a moment to flush summary/logs
            print(f"[JMeter] Allowing up to {jm_summary_wait}s for graceful shutdown …")
            jmeter_thread.join(timeout=jm_summary_wait)

            # 3) TERM → KILL fallback if still alive
            if jmeter_thread.is_alive():
                print("[JMeter] Still running; sending SIGTERM …")
                stop_jmeter_remote(jmeter_config)  # TERM by PID
                jmeter_thread.join(timeout=3)

            if jmeter_thread.is_alive():
                print("[JMeter] Still running; sending SIGKILL …")
                force_kill_jmeter_remote(jmeter_config)
                jmeter_thread.join(timeout=2)

            # 4) fetch all JMeter artifacts & clean remote originals
            include_logs = bool(jm.get("download_logs", True))  # default: True
            print("[JMeter] Fetching artifacts …")
            fetch_jmeter_artifacts(
                jmeter_config,
                local_output_root=output_dir,  # your existing ./output
                include_logs=include_logs,
                cleanup_remote=True,
            )

        print(f"[✓] Iteration {i} done.\n")

        # ── Post-hook (after JMeter is fully down) ────────────────────────────
        if after_experiment_hook:
            print(f"[~] Running post-experiment hook for {experiment_type}...")
            after_experiment_hook(hostname, username, password, config, output_dir)

        # JoularJX only writes total files after shutdown, so we need to download the data here
        joularjx_result_dir = exp.get('joularjx_result_dir')
        if joularjx_result_dir:
            fetch_joularjx_artifacts(
                exp,
                local_output_root=output_dir,  # your existing ./output
                cleanup_remote=True,
            )

        # ── Inter-iteration wait ──────────────────────────────────────────────
        if i < iterations and wait_between > 0:
            print(f"[~] Waiting {wait_between} seconds before next iteration...")
            time.sleep(wait_between)
