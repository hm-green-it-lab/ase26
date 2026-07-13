# ssh_temperatures.py
from __future__ import annotations

import re
from typing import Dict, Any


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
