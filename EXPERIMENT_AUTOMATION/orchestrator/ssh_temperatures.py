"""
ssh_temperatures.py

Read CPU package temperatures from a remote host via ``lm-sensors``.

The runner samples these once before and once after every measurement run and
records both values in ``logs/experiment_log.jsonl``, so that a repetition
started from an unusually warm machine can be recognized during analysis —
CPU temperature affects both leakage power and clock behaviour, and therefore
the measured energy.

Requires ``lm-sensors`` to be installed on the remote host; when it is not,
the reader degrades to an ``{"error": ...}`` result instead of failing the run.
"""
from __future__ import annotations

import re
from typing import Dict


def get_remote_temperatures(ssh) -> dict:
    """
    Read average CPU *package* temperatures (per socket) via `sensors`.

    Parameters
    ----------
    ssh : paramiko.SSHClient (or compatible)
        An established SSH client used to run the remote `sensors` command.

    Returns
    -------
    dict
        Mapping like {"Package_id_0": 45.0, "Package_id_1": 44.5}
        or {"error": "..."} if unavailable.

    Notes
    -----
    - Parses lines containing "Package id" from the `sensors` output.
    - Robust to optional '+' signs and varying whitespace.
    - Falls back to running raw `sensors` (without grep) if the first attempt
      yields no output.
    """
    try:
        # First try: filter remotely for performance/readability
        _, stdout, _ = ssh.exec_command("sensors | grep -i 'Package id'")
        output = stdout.read().decode(errors="replace")

        # Fallback: run full sensors if grep returned nothing (some systems differ)
        if not output.strip():
            _, stdout2, _ = ssh.exec_command("sensors")
            output = stdout2.read().decode(errors="replace")

        temps: Dict[str, float] = {}
        # Example line (varies by platform):
        # "Package id 0:  +45.0°C  (high = +80.0°C, crit = +100.0°C)"
        # Regex:
        #   group(1) -> index (optional)
        #   group(2) -> temperature number
        pkg_re = re.compile(r"(?i)\bPackage\s+id\s*(\d*)\s*:\s*\+?(-?\d+(?:\.\d+)?)")

        for line in output.splitlines():
            m = pkg_re.search(line)
            if not m:
                continue
            idx = m.group(1)
            label = f"Package_id_{idx}" if idx != "" else "Package_id"
            try:
                temps[label] = float(m.group(2))
            except ValueError:
                # Skip unparsable numbers but keep going
                continue

        return temps if temps else {"error": "no package temps found"}
    except Exception as e:
        return {"error": str(e)}
