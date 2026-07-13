"""
shared.py - Shared directory traversal for experiment-analysis scripts.

Directory layout::

    EXPERIMENT_RESULTS/
        Container/
            230/                 # run dirs
            230_run2/
            350/
            20250923_150700_spring_docker_tools/  # scenario subdirs
        VM/
            230/
            230_2/
            350/
            20260619_210721_spring_vm_scaphandre/  # scenario subdirs


Recommended pattern – analyse each environment (Container, VM, …)
separately so their data never mixes::

    from shared import discover_environments, build_run_dirs

    root = Path(__file__).resolve().parent
    for env_name in discover_environments(root):
        run_dirs = build_run_dirs(root, env_name)
        for load_level in sorted(run_dirs, key=int):  # "230", "350", …
            for run_dir in run_dirs[load_level]:
                for scenario_dir in run_dir.iterdir():
                    # scenario_dir is inside run_dir
                    …


If merging is truly desired, pass ``environment=None`` (default).
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

import numpy as np
import pandas as pd
from collections import defaultdict


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _is_run_dir(name: str) -> bool:
    """Return True if *name* looks like a run directory (e.g. ``350``,
    ``350_run2``)."""
    return name.split("_")[0].isdigit()


def _extract_tool_suffix(scenario_name: str) -> str:
    """Extract the tool suffix from a scenario directory name.

    Handles both ``spring_docker_*`` and ``spring_vm_*`` naming patterns.
    Examples:
        ``20251011_152909_spring_docker_kepler`` -> ``kepler``
        ``20260619_210721_spring_vm_scaphandre`` -> ``scaphandre``
    """
    parts = scenario_name.split("_")
    for part in reversed(parts):
        if part in TOOLS or part in ("none", "idle", "tools"):
            return part
    # Fallback: last part
    return parts[-1] if parts else ""


# ---------------------------------------------------------------------------
# Power data loading / parsing (shared across most analysis scripts)
# ---------------------------------------------------------------------------

def trim_time_series(
    df: pd.DataFrame,
    trim_seconds: float = 0,
    jmeter_bounds: tuple | None = None,
) -> pd.DataFrame:
    """Remove records outside a time window.

    If *jmeter_bounds* ``(start, end)`` is provided (both not ``None``),
    only rows with ``df['datetime']`` in that range are returned.
    Otherwise a symmetric relative trim is applied: the first and last
    *trim_seconds* seconds of the series are discarded.
    """
    if jmeter_bounds is not None and all(jmeter_bounds):
        start, end = jmeter_bounds
        return df[(df["datetime"] >= start) & (df["datetime"] <= end)]
    if trim_seconds <= 0:
        return df
    start_time = df["datetime"].min()
    end_time = df["datetime"].max()
    cutoff_start = start_time + pd.Timedelta(seconds=trim_seconds)
    cutoff_end = end_time - pd.Timedelta(seconds=trim_seconds)
    return df[(df["datetime"] >= cutoff_start) & (df["datetime"] <= cutoff_end)]


def load_rittal_data(
    file_path: str,
    trim_seconds: float = 0,
    jmeter_bounds: tuple | None = None,
) -> pd.DataFrame:
    """Load Rittal CSV, sum power per timestamp, return ``datetime``+``Power (Watts)``."""
    df = pd.read_csv(file_path)
    df["Timestamp"] = pd.to_numeric(df["Timestamp"], errors="coerce")
    df = df.dropna(subset=["Timestamp"])
    power_data = df.groupby("Timestamp")["Power (Watts)"].sum().reset_index()
    power_data["datetime"] = pd.to_datetime(power_data["Timestamp"], unit="ms")
    if trim_seconds > 0 or jmeter_bounds is not None:
        power_data = trim_time_series(power_data, trim_seconds, jmeter_bounds=jmeter_bounds)
    return power_data


def calculate_power_from_energy(
    df: pd.DataFrame,
    trim_seconds: float = 0,
    jmeter_bounds: tuple | None = None,
) -> pd.DataFrame:
    """Calculate power (W) from energy (µJ) in powercap‑style CSV data.

    Parameters
    ----------
    df :
        DataFrame with columns ``Timestamp`` (epoch ms) and
        `` Energy (micro joules)`` and ``Domain``.
    verbose :
        When ``True``, prints diagnostics for every negative‑power sample.
    """
    df = df.sort_values(["Timestamp", "Domain"])
    df["datetime"] = pd.to_datetime(df["Timestamp"], unit="ms")
    power_data = []
    for domain in df["Domain"].unique():
        domain_data = df[df["Domain"] == domain].copy()
        energy_diff = domain_data[" Energy (micro joules)"].diff()
        time_diff = domain_data["datetime"].diff().dt.total_seconds()
        power = energy_diff / (time_diff * 1_000_000)
        if (power < 0).any():
            negative_indices = power[power < 0].index
            for idx in negative_indices:
                _print_verbose_negative_power(domain, domain_data, energy_diff, time_diff, power, idx)
                power[idx] = np.nan
        domain_data["Power"] = power
        domain_data = domain_data.dropna(subset=["Power"])
        power_data.append(domain_data)
    result = pd.concat(power_data)
    domain_counts = result.groupby("datetime")["Domain"].count()
    complete_timestamps = domain_counts[domain_counts == len(df["Domain"].unique())].index
    result_filtered = result[result["datetime"].isin(complete_timestamps)]
    power_sum = result_filtered.groupby("datetime")["Power"].sum().reset_index()
    if trim_seconds > 0 or jmeter_bounds is not None:
        power_sum = trim_time_series(power_sum, trim_seconds, jmeter_bounds=jmeter_bounds)
    return power_sum


def load_power_data(
    file_path: str,
    trim_seconds: float = 0,
    jmeter_bounds: tuple | None = None,
) -> pd.DataFrame:
    """Dispatch to ``load_rittal_data`` or ``calculate_power_from_energy``."""
    if "rittal" in file_path.lower():
        return load_rittal_data(file_path, trim_seconds, jmeter_bounds=jmeter_bounds)
    elif "powercap" in file_path.lower():
        df = pd.read_csv(file_path)
        return calculate_power_from_energy(df, trim_seconds, jmeter_bounds=jmeter_bounds)
    else:
        raise ValueError(f"Unknown file type: {file_path}")


def get_jmeter_time_bounds(jmeter_dir, trim_seconds):
    """Find the jmeter .jtl file in *jmeter_dir*, read the ``timeStamp`` column,
    and return ``(trimmed_start, trimmed_end)`` as ``pandas.Timestamp``,
    or ``(None, None)`` if not found.
    """
    jtl_file = None
    for root, dirs, files in os.walk(jmeter_dir):
        for f in files:
            if f.endswith(".jtl"):
                jtl_file = os.path.join(root, f)
                break
        if jtl_file:
            break
    if not jtl_file:
        return None, None
    try:
        df = pd.read_csv(jtl_file, low_memory=False)
        if df.empty or "timeStamp" not in df.columns:
            return None, None
        min_ts = df["timeStamp"].min()
        max_ts = df["timeStamp"].max()
        min_time = pd.to_datetime(min_ts, unit="ms")
        max_time = pd.to_datetime(max_ts, unit="ms")
        trimmed_start = min_time + pd.Timedelta(seconds=trim_seconds)
        trimmed_end = max_time - pd.Timedelta(seconds=trim_seconds)
        return trimmed_start, trimmed_end
    except Exception as e:
        print(f"Error reading jmeter file {jtl_file}: {e}")
        return None, None


def extract_service_pids(experiment_log_path: str) -> list[str]:
    """Read *experiment_log_path* (``.jsonl``) and return ``service_pids`` as strings."""
    collected: list[str] = []
    try:
        with open(experiment_log_path, "r", encoding="utf-8") as f:
            for line in f:
                try:
                    entry = json.loads(line)
                    if "service_pids" in entry:
                        collected.extend([str(pid) for pid in entry["service_pids"]])
                except Exception:
                    continue
    except Exception as e:
        print(f"Error reading {experiment_log_path}: {e}")
    return collected


# ---------------------------------------------------------------------------
# Discover / build
# ---------------------------------------------------------------------------

def discover_environments(root: Path) -> list[str]:
    """Return a sorted list of environment subdirectory names
    (e.g. ``["Container", "VM"]``).

    An environment directory is one that:
    - is **not** named with a numeric prefix (so it is not a flat run dir), and
    - contains at least one run directory as a direct child.
    """
    envs: list[str] = []
    for child in sorted(root.iterdir()):
        if not child.is_dir():
            continue
        if _is_run_dir(child.name):
            continue
        if any(_is_run_dir(c.name) for c in child.iterdir() if c.is_dir()):
            envs.append(child.name)
    return envs


def build_run_dirs(root: Path, *, environment: str | None = None) -> dict[str, list[Path]]:
    """Return ``{load_level: [run_dir, …]}``.

    Each ``run_dir`` points to a concrete directory that holds scenario
    subdirectories (e.g. ``Container/350``, ``Container/230_run2``, or
    flat ``480``).  The *scenario* directories themselves are found by
    calling ``run_dir.iterdir()`` — this function does **not** look
    inside them.

    Parameters
    ----------
    root :
        Path to the ``EXPERIMENT_RESULTS`` directory (i.e. the script's
        parent directory).
    environment :
        Restrict to one environment (``"Container"``, ``"VM"``, …).
        When ``None`` (default), both flat dirs and nested ones are
        merged.  Pass a specific name to analyse that environment alone.

    Example – separate analysis::

        envs = discover_environments(root)   # ["Container", "VM"]
        for env in envs:
            runs = build_run_dirs(root, environment=env)
            for level in sorted(runs, key=int):
                for run_dir in runs[level]:
                    for scenario_dir in run_dir.iterdir():
                        # …

    Example – legacy merge (not recommended)::

        runs = build_run_dirs(root)
    """
    level_map: dict[str, list[Path]] = defaultdict(list)
    candidates: list[Path] = []

    if environment is not None:
        # Scoped: only inside the named environment subdir.
        env_dir = root / environment
        if env_dir.is_dir():
            candidates = [d for d in env_dir.iterdir() if d.is_dir()]
    else:
        # Full scan: flat + one level deep.
        for child in root.iterdir():
            if child.is_dir():
                candidates.append(child)
        for env_dir in root.iterdir():
            if env_dir.is_dir() and not _is_run_dir(env_dir.name):
                candidates.extend(
                    d for d in env_dir.iterdir() if d.is_dir()
                )

    for directory in candidates:
        prefix = directory.name.split("_")[0]
        if prefix.isdigit():
            level_map[prefix].append(directory)

    return {k: sorted(v) for k, v in level_map.items()}


def _print_verbose_negative_power(domain, domain_data, energy_diff, time_diff, power, idx):
    """Print verbose diagnostics for negative RAPL power (used by visualizeIdlePowerConsumptionAsBoxPlot)."""
    print(f"\nNegative power value found for Domain {domain}:")
    print(f"Timestamp: {domain_data.loc[idx, 'datetime']} {domain_data.loc[idx, 'Timestamp']}")
    print(f"Energy diff: {energy_diff[idx]} \u00b5J")
    print(f"Time diff: {time_diff[idx]} s")
    print(f"Calculated power: {power[idx]} W")
    print("\nInvolved records:")
    print(f"Current record:")
    print(domain_data.loc[idx, [" Energy (micro joules)", "datetime"]].to_string())
    print(f"Skipping negative power value for Domain {domain} as RAPL might have overflown")


# ---------------------------------------------------------------------------
# HTTP logger parsers (identical between Container and Process scripts)
# ---------------------------------------------------------------------------

def parse_scaphandre_http_logger(
    file_path: str,
    service_pids,
    trim_seconds: float = 0,
    jmeter_bounds: tuple | None = None,
    split_containers: bool = False,
) -> pd.DataFrame:
    """Parse Scaphandre http_logger CSV, return per-pid power series."""
    if split_containers:
        if len(service_pids) != 2:
            raise ValueError(
                f"Expected exactly 2 service PIDs but got {len(service_pids)}"
            )

        return [
            parse_scaphandre_http_logger(
                file_path,
                [service_pids[0]],
                trim_seconds,
                jmeter_bounds,
                split_containers=False
            ),
            parse_scaphandre_http_logger(
                file_path,
                [service_pids[1]],
                trim_seconds,
                jmeter_bounds,
                split_containers=False
            )
        ]


    data = []
    current_timestamp = None
    data_line_re = re.compile(r"^DATA:.* at (\d+)")
    scaphandre_line_re = re.compile(
        r'scaph_process_power_consumption_microwatts\{[^}]*pid="(\d+)"[^}]*\} ([\d\.eE+-]+)'
    )
    with open(file_path, "r", encoding="utf-8", errors="ignore") as fh:
        for line in fh:
            m = data_line_re.match(line)
            if m:
                current_timestamp = int(m.group(1))
                continue
            if current_timestamp is not None:
                sm = scaphandre_line_re.match(line)
                if sm:
                    pid, value = sm.group(1), sm.group(2)
                    if pid in service_pids:
                        dt = pd.to_datetime(current_timestamp, unit="ms")
                        data.append({"datetime": dt, "Power": float(value) / 1_000_000})
    df = pd.DataFrame(data)
    if not df.empty and (trim_seconds > 0 or jmeter_bounds is not None):
        df = trim_time_series(df, trim_seconds, jmeter_bounds=jmeter_bounds)
    return df


def filter_application_powerapi_files(file_paths: list) -> list:
    """Return only the application-container PowerReport.csv paths, discarding
    monitoring-tool containers such as the HWPC sensor.

    When SmartWatts is used, both the test-application container and the HWPC
    sensor container sit inside the same cgroup slice, so the file glob returns
    a CSV for each.  The monitoring container consumes far less power than the
    application under load.  Any container whose mean reported power is below
    5 % of the maximum mean power across all candidates is treated as a
    monitoring tool and excluded.

    A single file is always returned unchanged.
    """
    if len(file_paths) <= 1:
        return list(file_paths)

    mean_powers: dict[str, float] = {}
    for path in file_paths:
        try:
            df = pd.read_csv(str(path))
            if "power" in df.columns and not df.empty:
                mean_powers[str(path)] = float(df["power"].mean())
            else:
                mean_powers[str(path)] = 0.0
        except Exception:
            mean_powers[str(path)] = 0.0

    max_power = max(mean_powers.values()) if mean_powers else 0.0
    if max_power <= 0:
        return list(file_paths)

    threshold = 0.05 * max_power
    return [p for p in file_paths if mean_powers.get(str(p), 0.0) >= threshold]


def parse_powerapi_report(
    file_path: str,
    trim_seconds: float = 0,
    jmeter_bounds: tuple | None = None,
) -> pd.DataFrame:
    """Parse SmartWatts PowerReport CSV, aggregate to per-timestamp power."""
    df = pd.read_csv(file_path)
    if "timestamp" not in df.columns or "power" not in df.columns:
        return pd.DataFrame(columns=["datetime", "Power"])

    power_data = df[["timestamp", "power"]].copy()
    power_data["timestamp"] = pd.to_numeric(power_data["timestamp"], errors="coerce")
    power_data = power_data.dropna(subset=["timestamp"])
    power_data["datetime"] = pd.to_datetime(power_data["timestamp"], unit="ms")
    power_data = (
        power_data.groupby("datetime")["power"].sum().reset_index()
    )
    power_data = power_data.rename(columns={"power": "Power"})

    if trim_seconds > 0 or jmeter_bounds is not None:
        power_data = trim_time_series(
            power_data, trim_seconds, jmeter_bounds=jmeter_bounds
        )
    return power_data


def parse_powerapi_reports(
    file_paths: list,
    trim_seconds: float = 0,
    jmeter_bounds: tuple | None = None,
) -> pd.DataFrame:
    """Parse multiple SmartWatts PowerReport CSVs and return a single combined
    per-timestamp power DataFrame.

    Monitoring-tool containers (e.g. the HWPC sensor) are excluded via
    :func:`filter_application_powerapi_files` before aggregation.  If more
    than one application container remains their per-timestamp power values
    are summed.
    """
    filtered = filter_application_powerapi_files(file_paths)
    if not filtered:
        return pd.DataFrame(columns=["datetime", "Power"])

    dfs = [parse_powerapi_report(str(p), trim_seconds, jmeter_bounds) for p in filtered]
    dfs = [d for d in dfs if not d.empty]
    if not dfs:
        return pd.DataFrame(columns=["datetime", "Power"])
    if len(dfs) == 1:
        return dfs[0]

    combined = pd.concat(dfs)
    combined = combined.groupby("datetime")["Power"].sum().reset_index()
    return combined


# ---------------------------------------------------------------------------
# Scenario matching helpers (Container vs VM naming patterns)
# ---------------------------------------------------------------------------

TOOLS = frozenset(["tools", "kepler", "scaphandre", "otjae", "joularjx", "powerapi"])

_SCENARIO_SUFFIXES: dict[str, list[str]] = {
    "tools":      ["idle_no_tools", "idle", "none", "tools"],
    "kepler":     ["kepler"],
    "scaphandre": ["scaphandre"],
    "otjae":      ["otjae"],
    "joularjx":   ["joularjx"],
    "powerapi":   ["powerapi"],
    "idle":       ["idle_no_tools", "idle", "none"],
}


def strip_rs_suffix(scenario_dir_name: str) -> str:
    """Remove a trailing runtime-setup marker (``_rs2``, ``_rs3``, …) from a
    scenario directory name so the tool suffix becomes the last component
    again (e.g. ``…_spring_docker_kepler_rs2`` -> ``…_spring_docker_kepler``).
    """
    return re.sub(r"_rs\d+$", "", scenario_dir_name, flags=re.IGNORECASE)


def scenario_matches(scenario_dir_name: str, category: str) -> bool:
    """Return True if *scenario_dir_name* belongs to *category*.

    Handles both ``spring_docker_*`` and ``spring_vm_*`` naming patterns,
    including RS2/RS3 scenario names with a trailing ``_rs2``/``_rs3`` marker.
    Categories: tools, kepler, scaphandre, otjae, joularjx, powerapi, idle.
    """
    scenario_dir_name = strip_rs_suffix(scenario_dir_name)
    if category not in _SCENARIO_SUFFIXES:
        return False
    allowed = _SCENARIO_SUFFIXES[category]
    # Check exact suffix match
    if any(scenario_dir_name.endswith(sfx) for sfx in allowed):
        return True
    # Check spring_docker_* or spring_vm_* patterns
    for sfx in allowed:
        if scenario_dir_name.endswith(f"spring_docker_{sfx}"):
            return True
        if scenario_dir_name.endswith(f"spring_vm_{sfx}"):
            return True
        if scenario_dir_name.endswith(f"docker_{sfx}"):
            return True
        if scenario_dir_name.endswith(f"vm_{sfx}"):
            return True
    return False


Scenario = dict  # {load_level: dict[scenario_name, data]}


def scenario_matches_any(scenario_dir_name: str, suffixes: list[str]) -> bool:
    """Return True if *scenario_dir_name* matches any of the given suffix strings.

    Each suffix is a category name (e.g. ``"tools"``, ``"kepler"``) that is
    looked up via :func:`scenario_matches`.
    """
    return any(scenario_matches(scenario_dir_name, cat) for cat in suffixes)


def get_procfs_glob_pattern(scenario_dir_name: str) -> str:
    """Return the glob pattern for procfs files matching *scenario_dir_name*.

    Extracts the tool suffix and returns a pattern that matches both
    ``procfs_spring_docker_<tool>_*`` and ``procfs_spring_vm_<tool>_*``.
    """
    tool = _extract_tool_suffix(scenario_dir_name)
    return f"**/procfs_*_{tool}*.csv"


def get_http_logger_glob_pattern(scenario_dir_name: str) -> str:
    """Return the glob pattern for http_logger files matching *scenario_dir_name*.

    Extracts the tool suffix and returns a pattern that matches both
    ``http_logger_spring_docker_<tool>*`` and ``http_logger_spring_vm_<tool>*``.
    """
    tool = _extract_tool_suffix(scenario_dir_name)
    return f"**/http_logger_*_{tool}*.csv"


def docker_or_vm_endswith(scenario_dir_name: str, tool: str) -> bool:
    """Return True if the scenario name ends with a docker/VM variant of *tool*.

    Matches patterns like::
        docker_tools, spring_docker_tools, spring_vm_tools
        docker_kepler, spring_docker_kepler, spring_vm_kepler
        etc.
    """
    return (
        scenario_dir_name.endswith(f"docker_{tool}")
        or scenario_dir_name.endswith(f"spring_docker_{tool}")
        or scenario_dir_name.endswith(f"spring_vm_{tool}")
    )
