"""
fig_rs2_rs3.py

Estimate per-process power consumption for the RS2/RS3 multi-container load-distribution experiments and generate boxplots grouped by load split (50/50, 67/33, 80/20). This script contains helpers to parse procfs, powercap and other experiment logs and uses attribution models to distribute system-level power to processes (e.g., using procfs CPU fractions) and to add memory/storage power contributions.

Constants
- MEMORY_POWER_W_PER_GB, NETWORK_POWER_W_PER_GB, STORAGE_POWER_W_PER_TB are used as conversion factors for memory, network and storage demands.
"""

# Global constants for OTJAE power calculations
MEMORY_POWER_W_PER_GB = 0.392
NETWORK_POWER_W_PER_GB = 1.0
STORAGE_POWER_W_PER_TB = 1.2

import re

import pandas as pd
import numpy as np
import matplotlib.pyplot as plt
from matplotlib.patches import Patch
from pathlib import Path
from collections import defaultdict
import json

from shared import (
    build_run_dirs,
    trim_time_series,
    load_rittal_data,
    load_power_data,
    get_jmeter_time_bounds,
    extract_service_pids,
    parse_scaphandre_http_logger,
    parse_joularjx_power,
    parse_powerapi_report,
    parse_powerapi_reports,
    filter_application_powerapi_files,
    discover_environments,
    scenario_matches,
    scenario_matches_any,
    strip_rs_suffix,
    get_procfs_glob_pattern,
    get_http_logger_glob_pattern,
    docker_or_vm_endswith,
    summarize_repetitions,
    cohens_d_paired,
    wilcoxon_signed_rank_exact,
    fmt_mean_std,
)


# ---------------------------------------------------------------------------
# RS2/RS3 helpers (two containers per scenario, load-distribution variants)
# ---------------------------------------------------------------------------

# Boxplot face colors per tool (only used for RS2/RS3 figures; RS1 figures
# keep the uniform lightgrey styling).
TOOL_COLORS = {
    "rittal": "darkgrey",
    "powercap": "lightgrey",
    "kepler": "#b2df8a",
    "scaphandre": "#fb9a99",
    "powerapi": "#fdbf6f",
    "joularjx": "#a6cee3",
    "joularjx_procfs": "#1f78b4",
    "otjae": "#cab2d6",
}

# Hatch pattern distinguishing container 2 boxes from container 1 boxes.
CONTAINER2_HATCH = "//"

# RS2/RS3 run-dir naming: ``<load>_rs<N>_c1_<pct>_c2_<pct>[_run<k>]``, e.g.
# ``350_rs2_c1_67_c2_33`` or ``350_rs2_c1_67_c2_33_run2`` for repeated runs.
_RS_VARIANT_RE = re.compile(
    r"^(\d+)_rs\d+_c1_(\d+)_c2_(\d+)(?:_run\d+)?$", flags=re.IGNORECASE
)


def is_split_scenario(scenario_name: str) -> bool:
    """RS2/RS3 scenarios run two containers and are evaluated per container."""
    return bool(re.search(r"_rs[23]$", scenario_name, flags=re.IGNORECASE))


def strip_run_suffix(run_dir_name: str) -> str:
    """Strip a trailing run-repetition suffix from a run directory name and
    normalize RS2/RS3 load-distribution variants to a canonical key.

    ``350_run2`` -> ``350``, ``560`` -> ``560``,
    ``350_rs2_c1_67_c2_33_run2`` -> ``350_c1_67_c2_33``.
    """
    m = _RS_VARIANT_RE.match(run_dir_name)
    if m:
        load, c1_pct, c2_pct = m.groups()
        return f"{load}_c1_{c1_pct}_c2_{c2_pct}"
    return re.sub(r"_(?:run)?\d+$", "", run_dir_name)


def regroup_by_variant(load_level_map: dict) -> dict:
    """Regroup ``{load_level: [run_dir, …]}`` so that load-distribution
    variants (e.g. ``350_c1_67_c2_33`` vs ``350_c1_80_c2_20``) become
    separate keys instead of being merged under their numeric prefix.
    """
    regrouped = defaultdict(list)
    for dirs in load_level_map.values():
        for d in dirs:
            regrouped[strip_run_suffix(d.name)].append(d)
    return {k: sorted(v) for k, v in regrouped.items()}


def is_load_key(key: str) -> bool:
    """True for load-level keys (``350``, ``480_c2-33``, …), False for
    auxiliary keys such as ``pcpumin``/``pcpumax``."""
    return bool(re.match(r"^\d+", key))


def parse_load_key(key: str) -> tuple:
    """Split a load-level key into ``(numeric_load, variant)``.

    ``480_c2-33`` -> ``(480, "c2-33")``, ``350`` -> ``(350, "")``.
    """
    m = re.match(r"^(\d+)(?:_(.+))?$", key)
    if not m:
        return (0, key)
    return int(m.group(1)), m.group(2) or ""


_DISTRIBUTION_VARIANT_RE = re.compile(r"^c1_(\d+)_c2_(\d+)$")


def distribution_label(variant: str) -> str:
    """Return the container 1 / container 2 load distribution for a variant."""
    m = _DISTRIBUTION_VARIANT_RE.match(variant)
    if m:
        return f"{m.group(1)}/{m.group(2)}"
    return "50/50"


def scenario_endswith(scenario_name: str, suffix: str) -> bool:
    """Suffix match that also works for ``…_rs2``/``…_rs3`` scenario names."""
    return strip_rs_suffix(scenario_name).endswith(suffix)


def parse_kepler_http_logger(
        file_path,
        service_pids,
        trim_seconds=0,
        jmeter_bounds=None,
        split_containers=False):
    """
    Parses a large http_logger_spring_docker_kepler file, extracts kepler_process_cpu_watts for the given service_pids.
    Only includes values within the jmeter_bounds timeframe if provided.
    Reads the file in a single pass regardless of split_containers, since it can be 100+ MB
    and re-scanning it once per container PID would double the I/O and regex work.
    Returns a DataFrame with columns: ['datetime', 'Power'], or - when split_containers - a
    list of two such DataFrames (one per service PID).
    """
    if split_containers and len(service_pids) != 2:
        raise ValueError(
            f"Expected exactly 2 service PIDs but got {len(service_pids)}"
        )
    pid_set = set(service_pids)
    # Regex for DATA line and kepler metric line
    data_line_re = re.compile(r"^DATA:.* at (\d+)")
    kepler_line_re = re.compile(r'kepler_process_cpu_watts\{[^}]*pid="(\d+)"[^}]*\} ([\d\.eE+-]+)')
    records = []
    current_timestamp = None
    with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
        for line in f:
            m = data_line_re.match(line)
            if m:
                current_timestamp = int(m.group(1))
                continue
            if current_timestamp is not None:
                km = kepler_line_re.match(line)
                if km:
                    pid, value = km.group(1), km.group(2)
                    if pid in pid_set:
                        records.append((pid, current_timestamp, float(value)))

    def build_df(pids):
        rows = [
            {'datetime': pd.to_datetime(ts, unit='ms'), 'Power': value}
            for pid, ts, value in records
            if pid in pids
        ]
        df = pd.DataFrame(rows, columns=['datetime', 'Power'])
        # Optionally trim the time series
        if not df.empty and (trim_seconds > 0 or jmeter_bounds is not None):
            df = trim_time_series(df, trim_seconds, jmeter_bounds)
        return df

    if split_containers:
        return [build_df({service_pids[0]}), build_df({service_pids[1]})]
    return build_df(pid_set)

# New helper function to parse procfs file and return process and system CPU utilization values
def parse_procfs_data(procfs_file, service_pids, n_cores=80, ticks_per_sec=100, jmeter_bounds=None):
    """
    Parses procfs CSV and returns:
      - proc_util: DataFrame with ['datetime', 'util_ratio'] (summed across all service_pids)
      - sys_df: DataFrame with system CPU utilization info
      - mem_deltas: DataFrame with ['datetime', 'pid', 'delta_vmsize', 'delta_vmrss']
      - io_deltas: DataFrame with ['datetime', 'pid', 'delta_read_bytes', 'delta_write_bytes']
    """
    import pandas as pd
    try:
        df = pd.read_csv(procfs_file)
        # Determine steady-state window (jmeter_bounds) or fallback to trim 60s from start/end
        min_time = pd.to_datetime(df['Timestamp'].min(), unit='ms')
        max_time = pd.to_datetime(df['Timestamp'].max(), unit='ms')
        trim_seconds = 60
        if jmeter_bounds is not None and all(jmeter_bounds):
            start, end = jmeter_bounds
        else:
            start = min_time + pd.Timedelta(seconds=trim_seconds)
            end = max_time - pd.Timedelta(seconds=trim_seconds)
        # Filter df to steady-state window
        df = df[(pd.to_datetime(df['Timestamp'], unit='ms') >= start) & (pd.to_datetime(df['Timestamp'], unit='ms') <= end)]
        # --- CPU (stat) ---
        stat_mask = df['SourceFile'].str.endswith(r'stat')
        stat_df = df[stat_mask].copy()
        stat_df['pid'] = stat_df['SourceFile'].str.extract(r'/proc/(\d+)/stat')
        stat_df['datetime'] = pd.to_datetime(stat_df['Timestamp'], unit='ms')
        stat_df['userTime_s'] = stat_df['userTime (Ticks)'] / ticks_per_sec
        stat_df['systemTime_s'] = stat_df['systemTime (Ticks)'] / ticks_per_sec
        sys_df = stat_df[stat_df['SourceFile'] == '/proc/stat'].sort_values('datetime')
        proc_data = stat_df[stat_df['SourceFile'] != '/proc/stat']
        proc_df = proc_data[proc_data['pid'].isin(service_pids)].sort_values(['pid', 'datetime'])
        sys_df = sys_df[['datetime', 'userTime_s', 'systemTime_s']].copy()
        sys_df['total_cpu'] = sys_df['userTime_s'] + sys_df['systemTime_s']
        sys_df['delta_cpu'] = sys_df['total_cpu'].diff()
        sys_df['interval'] = sys_df['datetime'].diff().dt.total_seconds()
        sys_df = sys_df.iloc[1:]
        proc_power = []
        for pid in service_pids:
            pid_df = proc_df[proc_df['pid'] == pid][['datetime', 'userTime_s', 'systemTime_s']].copy()
            pid_df['total_cpu'] = pid_df['userTime_s'] + pid_df['systemTime_s']
            pid_df['delta_cpu'] = pid_df['total_cpu'].diff()
            pid_df['interval'] = pid_df['datetime'].diff().dt.total_seconds()
            pid_df = pid_df.iloc[1:]
            merged = pd.merge_asof(pid_df.sort_values('datetime'), sys_df.sort_values('datetime'), on='datetime', suffixes=('_proc', '_sys'), direction='nearest', tolerance=pd.Timedelta('1s'))
            merged['util_ratio'] = (merged['delta_cpu_proc'] / (merged['interval_proc'] * n_cores)) / (merged['delta_cpu_sys'] / (merged['interval_sys'] * n_cores))
            merged['util_ratio'] = merged['util_ratio'].clip(lower=0, upper=1)
            merged['pid'] = pid
            proc_power.append(merged[['datetime', 'pid', 'util_ratio']])
        if proc_power:
            proc_util = pd.concat(proc_power).groupby('datetime')['util_ratio'].sum().reset_index()
        else:
            proc_util = None
        # --- Memory (status) ---
        status_mask = df['SourceFile'].str.endswith(r'status')
        status_df = df[status_mask].copy()
        status_df['pid'] = status_df['SourceFile'].str.extract(r'/proc/(\d+)/status')
        status_df['datetime'] = pd.to_datetime(status_df['Timestamp'], unit='ms')
        mem_deltas = []
        for pid in service_pids:
            pid_status = status_df[status_df['pid'] == pid][['datetime', 'VmSize', 'VmRSS']].copy()
            pid_status = pid_status.sort_values('datetime')
            pid_status['delta_vmsize'] = pid_status['VmSize'].diff()
            pid_status['VmSize'] = pid_status['VmSize']
            pid_status['VmRSS'] = pid_status['VmRSS']
            pid_status['pid'] = pid
            mem_deltas.append(pid_status[['datetime', 'pid', 'VmSize', 'VmRSS']])
        if mem_deltas:
            mem_deltas_df = pd.concat(mem_deltas, ignore_index=True)
        else:
            mem_deltas_df = None
        # --- Storage (io) ---
        io_mask = df['SourceFile'].str.endswith(r'io')
        io_df = df[io_mask].copy()
        io_df['pid'] = io_df['SourceFile'].str.extract(r'/proc/(\d+)/io')
        io_df['datetime'] = pd.to_datetime(io_df['Timestamp'], unit='ms')
        io_deltas = []
        for pid in service_pids:
            pid_io = io_df[io_df['pid'] == pid][['datetime', 'read_bytes', 'write_bytes']].copy()
            pid_io = pid_io.sort_values('datetime')
            pid_io['delta_read_bytes'] = pid_io['read_bytes'].diff()
            pid_io['delta_write_bytes'] = pid_io['write_bytes'].diff()
            pid_io = pid_io.iloc[1:]
            pid_io['pid'] = pid
            io_deltas.append(pid_io[['datetime', 'pid', 'delta_read_bytes', 'delta_write_bytes']])
        if io_deltas:
            io_deltas_df = pd.concat(io_deltas, ignore_index=True)
        else:
            io_deltas_df = None
        return proc_util, sys_df, mem_deltas_df, io_deltas_df
    except Exception as e:
        print(f"Error in parse_procfs_data: {e}")
        return None, None, None, None


def parse_procfs_joularjx(
    procfs_file,
    powercap_files,
    service_pids,
    trim_seconds=0,
    jmeter_bounds=None,
    n_cores=80,
    ticks_per_sec=100,
    split_containers=False,
):
    """
    Parses procfs CSV and powercap CSVs, computes process-specific power
    consumption over time by attributing each host powercap sample to a
    process by its share of total system CPU ticks (proc_util['util_ratio']).

    Used as a cross-check/correction for JoularJX's own reported power ($P_{P_J}$):
    JoularJX derives per-process power from Java's JMX `OperatingSystemMXBean`
    CPU-load metrics, which don't reliably reflect cgroup CPU-quota throttling
    between co-located RS2/RS3 containers -- see the investigation that led
    here. This procfs-tick-based attribution is independent of JoularJX and
    correctly reflects each container's actual restricted CPU share.

    Returns a DataFrame with columns ['datetime', 'Power'] for the process,
    or -- when split_containers -- a list of two such DataFrames.
    """
    if split_containers:
        if len(service_pids) != 2:
            raise ValueError(
                f"Expected exactly 2 service PIDs but got {len(service_pids)}"
            )
        return [
            parse_procfs_joularjx(
                procfs_file, powercap_files, [service_pids[0]],
                trim_seconds, jmeter_bounds, n_cores, ticks_per_sec,
                split_containers=False,
            ),
            parse_procfs_joularjx(
                procfs_file, powercap_files, [service_pids[1]],
                trim_seconds, jmeter_bounds, n_cores, ticks_per_sec,
                split_containers=False,
            ),
        ]

    proc_util, sys_df, _, _ = parse_procfs_data(procfs_file, service_pids, n_cores=n_cores, ticks_per_sec=ticks_per_sec, jmeter_bounds=jmeter_bounds)
    if proc_util is None or sys_df is None:
        return pd.DataFrame(columns=['datetime', 'Power'])
    # Read and sum powercap power for both sockets at each timestamp
    powercap_power_list = []
    for f in powercap_files:
        try:
            df_power = load_power_data(str(f))
            if 'Power' in df_power.columns:
                powercap_power_list.append(df_power[['datetime', 'Power']])
            else:
                print(f"Power column not found after conversion in file: {f}")
        except Exception as e:
            print(f"Error processing powercap file {f}: {e}")
    if not powercap_power_list:
        print("No valid powercap power data found for JoularJX scenario.")
        return pd.DataFrame(columns=['datetime', 'Power'])
    powercap_df = pd.concat(powercap_power_list, ignore_index=True)
    powercap_df = powercap_df.groupby('datetime')['Power'].sum().reset_index()
    # Harmonize timestamps to seconds relative to steady-state start
    if jmeter_bounds is not None and jmeter_bounds[0] is not None:
        steady_state_start = jmeter_bounds[0]
    else:
        steady_state_start = proc_util['datetime'].min() if not proc_util.empty else None
    if steady_state_start is None:
        print("No steady-state start time found for harmonization.")
        return pd.DataFrame(columns=['datetime', 'Power'])
    proc_util = proc_util.copy()
    proc_util['rel_sec'] = (proc_util['datetime'] - steady_state_start).dt.total_seconds().astype(int)
    proc_util = proc_util[proc_util['rel_sec'] >= 0]
    powercap_df = powercap_df.copy()
    powercap_df['rel_sec'] = (powercap_df['datetime'] - steady_state_start).dt.total_seconds().astype(int)
    powercap_df = powercap_df[powercap_df['rel_sec'] >= 0]
    # Merge on rel_sec (exact match)
    merged = pd.merge(proc_util, powercap_df, on='rel_sec', how='inner', suffixes=('_proc', '_powercap'))
    merged['Power'] = merged['util_ratio'] * merged['Power']
    result = merged[['datetime_proc', 'Power']].rename(columns={'datetime_proc': 'datetime'}).dropna()
    if not result.empty and (trim_seconds > 0 or jmeter_bounds is not None):
        result = trim_time_series(result, trim_seconds, jmeter_bounds)
    return result


def process_docker_otjae(scenario_dir, trim_seconds, pcpumin, pcpumax, split_containers=False):
    # Find jmeter bounds
    jmeter_bounds = get_jmeter_time_bounds(str(scenario_dir), trim_seconds)
    # Find procfs file - match both spring_docker and spring_vm
    procfs_files = list(scenario_dir.glob('**/procfs_*_otjae_*.csv'))
    if not procfs_files:
        return None
    is_vm_env = "spring_vm" in scenario_dir.name
    # guest procfs files contain "_vm_" twice
    if is_vm_env:
        procfs_files = [
            f for f in procfs_files
            if Path(f).name.count("_vm_") >= 2
        ]
    if not procfs_files:
        return None
    procfs_file = str(procfs_files[0])
    # Find service_pids
    experiment_log_path = scenario_dir / 'logs' / 'experiment_log.jsonl'
    service_pids = extract_service_pids(str(experiment_log_path)) if experiment_log_path.exists() else []
    if not service_pids:
        return None
    # RS2/RS3: evaluate both containers independently
    if split_containers:
        if len(service_pids) != 2:
            raise ValueError(
                f"Expected exactly 2 service PIDs for RS2 or RS3 but got {len(service_pids)}: {service_pids}"
            )
        service_pid_sets = [
            [service_pids[0]],   # Container 1
            [service_pids[1]]    # Container 2
        ]
    else:
        service_pid_sets = [service_pids]
    results = []
    for current_service_pids in service_pid_sets:
        # Parse procfs data (get system utilization)
        proc_util, sys_df, mem_deltas_df, io_deltas_df = parse_procfs_data(
            procfs_file,
            current_service_pids,
            jmeter_bounds=jmeter_bounds
        )
        if sys_df is None or sys_df.empty:
            results.append(pd.Series(dtype=float))
            continue
        # Calculate system CPU utilization per second
        sys_df = sys_df.copy()
        sys_df['sys_util'] = sys_df['delta_cpu'] / (sys_df['interval'])
        # Normalize to [0,1] by dividing by n_cores (assume 80 as default)
        n_cores = 80
        sys_df['sys_util_norm'] = sys_df['sys_util'] / n_cores
        sys_df['sys_util_norm'] = sys_df['sys_util_norm'].clip(lower=0, upper=1)
        # Calculate power
        sys_df['Power'] = pcpumin + (
            sys_df['sys_util_norm'] * (pcpumax - pcpumin)
        )
        # RS2/RS3: with two co-located containers the host CPU power must be
        # attributed per container by its share of the total CPU time (Eq. 9),
        # otherwise each container would be assigned the full host power.
        if split_containers:
            if proc_util is None or proc_util.empty:
                results.append(pd.Series(dtype=float))
                continue
            share = proc_util.rename(columns={'util_ratio': 'container_share'})
            sys_df = pd.merge_asof(
                sys_df.sort_values('datetime'),
                share[['datetime', 'container_share']].sort_values('datetime'),
                on='datetime',
                direction='nearest',
                tolerance=pd.Timedelta('1s')
            )
            sys_df['container_share'] = sys_df['container_share'].fillna(0)
            sys_df['Power'] *= sys_df['container_share']
        # Add memory power (VmRSS in kB to GB)
        if mem_deltas_df is not None and not mem_deltas_df.empty:
            mem_group = (
                mem_deltas_df
                .groupby('datetime')['VmRSS']
                .sum()
                .reset_index()
            )
            mem_group['VmRSS_GB'] = mem_group['VmRSS'] / (1024 * 1024)
            mem_group['Pmemory'] = (
                mem_group['VmRSS_GB'] * MEMORY_POWER_W_PER_GB
            )
            sys_df = pd.merge_asof(
                sys_df.sort_values('datetime'),
                mem_group[['datetime', 'Pmemory']].sort_values('datetime'),
                on='datetime',
                direction='nearest',
                tolerance=pd.Timedelta('1s')
            )
            sys_df['Pmemory'] = sys_df['Pmemory'].fillna(0)
            sys_df['Power'] += sys_df['Pmemory']
        # Add storage power
        if io_deltas_df is not None and not io_deltas_df.empty:
            io_group = (
                io_deltas_df
                .groupby('datetime')[['delta_read_bytes', 'delta_write_bytes']]
                .sum()
                .reset_index()
            )
            io_group['total_bytes'] = (
                io_group['delta_read_bytes'].fillna(0)
                + io_group['delta_write_bytes'].fillna(0)
            )
            io_group['total_TB'] = io_group['total_bytes'] / (1024 ** 4)
            io_group['Pstorage'] = (
                io_group['total_TB'] * STORAGE_POWER_W_PER_TB
            )
            sys_df = pd.merge_asof(
                sys_df.sort_values('datetime'),
                io_group[['datetime', 'Pstorage']].sort_values('datetime'),
                on='datetime',
                direction='nearest',
                tolerance=pd.Timedelta('1s')
            )
            sys_df['Pstorage'] = sys_df['Pstorage'].fillna(0)
            sys_df['Power'] += sys_df['Pstorage']
        # We do not add network power as we do not have per-process network I/O data
        results.append(sys_df[['Power']].dropna()['Power'])
    if split_containers:
        return results
    return results[0] if results else None

def collect_data_by_load_level(trim_seconds=0, scenario_suffixes=None, included_load_levels=None, environments=None):
    """
    Collects and groups all Rittal and Powercap data by environment, load level (numeric prefix of directory name).
    Aggregates all runs (e.g., 350, 350_run2, 350_run3) for each load level within each environment.
    Load-distribution variants of RS2/RS3 (e.g. 480_c2-33 vs 480_c2-67) are kept as separate load-level keys.
    Only includes scenario subdirectories matching scenario_suffixes if provided.
    If included_load_levels is provided (list of strings), only load levels whose numeric prefix is in the
    list are included in the returned data (except for pcpumin/pcpumax, which always use all data).
    For RS2/RS3 scenarios (two containers), per-container results are stored under
    '<tool>_c1' / '<tool>_c2' keys instead of '<tool>'.

    environments: optional list of environment names (e.g. ["RS2", "RS3"]) to
    actually collect per-scenario data for. Restricting this speeds up
    processing considerably, since almost all runtime is spent parsing the
    procfs/JoularJX/HTTP-logger/PowerAPI files of the (expensive) per-scenario
    step below, not the (cheap) baseline-power lookup. RS2/RS3 have no
    uninstrumented "tools" baseline of their own, so pcpumin/pcpumax (needed
    for the OTJAE model) are always derived from ALL discovered environments
    first, regardless of this filter -- otherwise restricting *environments*
    to just RS2/RS3 would silently break their OTJAE column.

    Returns a dict: {environment: {load_level: {scenario: {'rittal': [series...], 'powercap': [series...]}, ...}, ...}}
    """
    exp_results = Path(__file__).resolve().parent
    all_env_names = discover_environments(exp_results)
    if not all_env_names:
        all_env_names = [None]
    env_names = [e for e in all_env_names if environments is None or e in environments]
    if not env_names:
        env_names = all_env_names

    # Outer structure: data_by_env[environment][load_level][scenario] = {rittal: [...], ...}
    data_by_env = {}

    # RS2/RS3 have no uninstrumented "tools" baseline, so pcpumin/pcpumax
    # cannot be derived there. Fall back to the values of the first
    # environment that provides them (same host hardware).
    fallback_pcpumin = None
    fallback_pcpumax = None

    # --- Step 1: derive pcpumin/pcpumax from the 'tools' baseline (cheap: only
    # reads a handful of powercap files) across ALL discovered environments,
    # regardless of *environments*, and cache each environment's (regrouped)
    # run-dir map for reuse in Step 2 below.
    pcpumin_by_env = {}
    pcpumax_by_env = {}
    load_level_map_by_env = {}
    for env_name in all_env_names:
        load_level_map = build_run_dirs(exp_results, environment=env_name) if env_name else build_run_dirs(exp_results)
        # Keep RS2/RS3 load-distribution variants separate (480_c2-33 vs 480_c2-67)
        load_level_map = regroup_by_variant(load_level_map)
        load_level_map_by_env[env_name] = load_level_map

        label = f"[{env_name}] " if env_name else ""

        pcpumin = None
        pcpumax = None
        for load_level in ['0', '560']:
            dirs = load_level_map.get(load_level, [])
            powercap_means = []
            for run_path in dirs:
                for scenario_dir in run_path.iterdir():
                    if not scenario_dir.is_dir():
                        continue
                    scenario_name = scenario_dir.name
                    if scenario_suffixes is not None and not scenario_matches_any(scenario_name, scenario_suffixes):
                        continue
                    if not scenario_matches(scenario_name, 'tools'):
                        continue
                    jmeter_bounds = get_jmeter_time_bounds(str(scenario_dir), trim_seconds)
                    powercap_files = list(scenario_dir.glob('**/powercap_*.csv'))
                    for file_path in powercap_files:
                        try:
                            power_data = load_power_data(str(file_path), trim_seconds, jmeter_bounds)
                            if 'Power' in power_data.columns:
                                powercap_means.append(power_data['Power'].mean())
                        except Exception as e:
                            print(f"Error loading {file_path}: {e}")
            if powercap_means:
                mean_val = float(np.mean(powercap_means))
                if load_level == '0':
                    pcpumin = mean_val
                elif load_level == '560':
                    pcpumax = mean_val
        if pcpumin is None:
            pcpumin = fallback_pcpumin
        if pcpumax is None:
            pcpumax = fallback_pcpumax
        if fallback_pcpumin is None:
            fallback_pcpumin = pcpumin
        if fallback_pcpumax is None:
            fallback_pcpumax = pcpumax
        pcpumin_by_env[env_name] = pcpumin
        pcpumax_by_env[env_name] = pcpumax
        print(f"{label}Min Mean CPU Power: {pcpumin} W, Max Mean CPU Power: {pcpumax} W")

    # --- Step 2: process the full per-scenario data, only for *environments* ---
    for env_name in env_names:
        load_level_map = load_level_map_by_env[env_name]
        data_by_load = defaultdict(lambda: defaultdict(lambda: defaultdict(list)))
        data_by_load['pcpumin'] = pcpumin_by_env.get(env_name, fallback_pcpumin)
        data_by_load['pcpumax'] = pcpumax_by_env.get(env_name, fallback_pcpumax)

        for load_level, dirs in load_level_map.items():
            # If included_load_levels is set, skip load levels not in the list (except for pcpumin/pcpumax)
            if included_load_levels is not None and load_level.split("_")[0] not in included_load_levels:
                continue
            for run_path in dirs:
                run_label = run_path.name
                for scenario_dir in run_path.iterdir():
                    if not scenario_dir.is_dir():
                        continue
                    scenario_name = scenario_dir.name
                    if scenario_suffixes is not None and not scenario_matches_any(scenario_name, scenario_suffixes):
                        continue
                    split = is_split_scenario(scenario_name)
                    jmeter_bounds = get_jmeter_time_bounds(str(scenario_dir), trim_seconds)
                    sdata = data_by_load[load_level][scenario_name]
                    # RS2/RS3 have no uninstrumented baseline scenario, so the
                    # powercap measurements taken during each tool scenario
                    # serve as the P_S reference for the plot and the table.
                    if split:
                        for file_path in scenario_dir.glob('**/powercap_*.csv'):
                            try:
                                power_data = load_power_data(str(file_path), trim_seconds, jmeter_bounds)
                                if 'Power' in power_data.columns:
                                    sdata['powercap'].append((run_label, power_data['Power']))
                            except Exception as e:
                                print(f"Error loading {file_path}: {e}")
                    # Load and append joularjx process power, computed directly from
                    # JoularJX's own per-second "all methods" power output rather
                    # than estimated from procfs CPU share and powercap.
                    if scenario_matches(scenario_name, 'joularjx'):
                        try:
                            joularjx_data = parse_joularjx_power(
                                scenario_dir,
                                trim_seconds=trim_seconds,
                                jmeter_bounds=jmeter_bounds,
                                split_containers=split,
                            )

                            if split:
                                for idx, container_df in enumerate(joularjx_data, start=1):
                                    if container_df is not None and not container_df.empty:
                                        sdata[f'joularjx_c{idx}'].append((run_label, container_df['Power']))
                            else:
                                if not joularjx_data.empty:
                                    sdata['joularjx'].append((run_label, joularjx_data['Power']))
                        except Exception as e:
                            print(f"Error loading joularjx result archive: {e}")
                        # Also compute the procfs-CPU-share x powercap based estimate
                        # ($P_{P_{JM}}$) as a cross-check/correction: JoularJX's own
                        # power attribution doesn't reliably reflect cgroup CPU-quota
                        # throttling between co-located RS2/RS3 containers.
                        procfs_files = list(scenario_dir.glob('**/procfs_*_joularjx_*.csv'))
                        powercap_files = list(scenario_dir.glob('**/powercap_*.csv'))
                        experiment_log_path = scenario_dir / 'logs' / 'experiment_log.jsonl'
                        service_pids = extract_service_pids(str(experiment_log_path)) if experiment_log_path.exists() else []
                        if procfs_files and powercap_files and service_pids:
                            try:
                                joularjx_procfs_data = parse_procfs_joularjx(
                                    str(procfs_files[0]),
                                    [str(f) for f in powercap_files],
                                    service_pids,
                                    trim_seconds=trim_seconds,
                                    jmeter_bounds=jmeter_bounds,
                                    split_containers=split,
                                )

                                if split:
                                    for idx, container_df in enumerate(joularjx_procfs_data, start=1):
                                        if container_df is not None and not container_df.empty:
                                            sdata[f'joularjx_procfs_c{idx}'].append((run_label, container_df['Power']))
                                else:
                                    if not joularjx_procfs_data.empty:
                                        sdata['joularjx_procfs'].append((run_label, joularjx_procfs_data['Power']))
                            except Exception as e:
                                print(f"Error loading joularjx procfs/powercap: {e}")
                    if scenario_matches(scenario_name, 'tools'):
                        rittal_files = list(scenario_dir.glob('**/rittal_*.csv'))
                        powercap_files = list(scenario_dir.glob('**/powercap_*.csv'))
                        for file_path in rittal_files:
                            try:
                                power_data = load_rittal_data(str(file_path), trim_seconds, jmeter_bounds)
                                if 'Power (Watts)' in power_data.columns:
                                    sdata['rittal'].append((run_label, power_data['Power (Watts)']))
                            except Exception as e:
                                print(f"Error loading {file_path}: {e}")
                        for file_path in powercap_files:
                            try:
                                power_data = load_power_data(str(file_path), trim_seconds, jmeter_bounds)
                                if 'Power' in power_data.columns:
                                    sdata['powercap'].append((run_label, power_data['Power']))
                            except Exception as e:
                                print(f"Error loading {file_path}: {e}")
                    kepler_files = list(scenario_dir.glob('**/http_logger_spring_docker_kepler*.csv'))
                    if kepler_files:
                        experiment_log_path = scenario_dir / 'logs' / 'experiment_log.jsonl'
                        service_pids = extract_service_pids(str(experiment_log_path)) if experiment_log_path.exists() else []
                        for file_path in kepler_files:
                            try:
                                kepler_data = parse_kepler_http_logger(
                                    str(file_path),
                                    service_pids,
                                    trim_seconds,
                                    jmeter_bounds,
                                    split_containers=split
                                )

                                if split:
                                    for idx, container_df in enumerate(kepler_data, start=1):
                                        if container_df is not None and not container_df.empty:
                                            sdata[f'kepler_c{idx}'].append((run_label, container_df['Power']))
                                else:
                                    if not kepler_data.empty:
                                        sdata['kepler'].append((run_label, kepler_data['Power']))
                            except Exception as e:
                                print(f"Error loading {file_path}: {e}")
                    scaphandre_files = list(scenario_dir.glob('**/http_logger_spring_docker_scaphandre*.csv')) + \
                                       list(scenario_dir.glob('**/http_logger_spring_vm_scaphandre*.csv'))
                    if scaphandre_files:
                        experiment_log_path = scenario_dir / 'logs' / 'experiment_log.jsonl'
                        service_pids = extract_service_pids(str(experiment_log_path)) if experiment_log_path.exists() else []
                        for file_path in scaphandre_files:
                            try:
                                scaphandre_data = parse_scaphandre_http_logger(
                                    str(file_path),
                                    service_pids,
                                    trim_seconds,
                                    jmeter_bounds,
                                    split_containers=split
                                )

                                if split:
                                    for idx, container_df in enumerate(scaphandre_data, start=1):
                                        if container_df is not None and not container_df.empty:
                                            sdata[f'scaphandre_c{idx}'].append((run_label, container_df['Power']))
                                else:
                                    if not scaphandre_data.empty:
                                        sdata['scaphandre'].append((run_label, scaphandre_data['Power']))
                            except Exception as e:
                                print(f"Error loading {file_path}: {e}")
                    if scenario_matches(scenario_name, 'powerapi'):
                        powerapi_c1_files = [
                            str(f) for f in scenario_dir.glob('**/smartwattsresults/localhost-/energy.slice/energy-consumption.slice/energy-consumption-experiment.slice/docker-*.scope/PowerReport.csv')
                        ]
                        powerapi_c2_files = [
                            str(f) for f in scenario_dir.glob('**/smartwattsresults/localhost-/energy.slice/energy-consumption.slice/energy-consumption-experiment.slice/energy-consumption-experiment-2.slice/docker-*.scope/PowerReport.csv')
                        ]
                        powerapi_c1_files = filter_application_powerapi_files(powerapi_c1_files)
                        powerapi_c2_files = filter_application_powerapi_files(powerapi_c2_files)
                        for file_path in powerapi_c1_files:
                            try:
                                powerapi_data = parse_powerapi_report(str(file_path), trim_seconds, jmeter_bounds)
                                if not powerapi_data.empty:
                                    if split:
                                        sdata['powerapi_c1'].append((run_label, powerapi_data['Power']))
                                    else:
                                        sdata['powerapi'].append((run_label, powerapi_data['Power']))
                            except Exception as e:
                                print(f"Error loading powerapi file {file_path}: {e}")
                        for file_path in powerapi_c2_files:
                            try:
                                powerapi_data = parse_powerapi_report(str(file_path), trim_seconds, jmeter_bounds)
                                if not powerapi_data.empty:
                                    if split:
                                        sdata['powerapi_c2'].append((run_label, powerapi_data['Power']))
                            except Exception as e:
                                print(f"Error loading powerapi file {file_path}: {e}")
                    if scenario_matches(scenario_name, 'otjae'):
                        pcpumin_ev = data_by_load.get('pcpumin', None)
                        pcpumax_ev = data_by_load.get('pcpumax', None)
                        if pcpumin_ev is not None and pcpumax_ev is not None:
                            try:
                                otjae_power = process_docker_otjae(
                                    scenario_dir,
                                    trim_seconds,
                                    pcpumin_ev,
                                    pcpumax_ev,
                                    split_containers=split
                                )

                                if split:
                                    for idx, container_series in enumerate(otjae_power or [], start=1):
                                        if container_series is not None and not container_series.empty:
                                            sdata[f'otjae_c{idx}'].append((run_label, container_series))
                                else:
                                    if otjae_power is not None and not otjae_power.empty:
                                        sdata['otjae'].append((run_label, otjae_power))
                            except Exception as e:
                                print(f"Error loading docker_otjae procfs/jmeter: {e}")

        data_by_env[env_name if env_name else 'flat'] = dict(data_by_load)

    return data_by_env


# Tool kinds in plotting order: (data key, math label, tool color)
_TOOL_PLOT_ORDER = [
    ("rittal", "$P_{EM}$", TOOL_COLORS["rittal"]),
    ("powercap", "$P_{S}$", TOOL_COLORS["powercap"]),
    ("kepler", "$P_{P_{K}}$", TOOL_COLORS["kepler"]),
    ("scaphandre", "$P_{P_{Sc}}$", TOOL_COLORS["scaphandre"]),
    ("powerapi", "$P_{P_{PA}}$", TOOL_COLORS["powerapi"]),
    ("joularjx", "$P_{P_{J}}$", TOOL_COLORS["joularjx"]),
    ("joularjx_procfs", "$P_{P_{JM}}$", TOOL_COLORS["joularjx_procfs"]),
    ("otjae", "$P_{P_{O}}$", TOOL_COLORS["otjae"]),
]


def _scenario_dict_has_split(scenario_dict):
    """True if any scenario in the dict holds per-container (_c1/_c2) data."""
    for sdata in scenario_dict.values():
        for key, values in sdata.items():
            if key.endswith(('_c1', '_c2')) and len(values) > 0:
                return True
    return False


def plot_all_load_levels_in_one_row(data_by_env, output_path, scenario_suffixes=None, custom_labels=None, show_rittal=True):
    """
    Plots a single row of boxplots, one for each load level, each showing Rittal and Powercap data for selected scenarios.
    Groups all data for each scenario (across all runs) into a single boxplot per scenario per load level.

    Parameters are environment-aware - expects data_by_env[env][load_level][scenario] structure.

    For RS2/RS3 data (per-container '<tool>_c1'/'<tool>_c2' keys), each tool gets two
    half-width boxplots side by side (container 1 and container 2). Boxes are colored
    per tool and container 2 boxes are hatched. The panel title additionally shows the
    load distribution between the two containers.

    scenario_suffixes: list of scenario suffixes to include and order.
    custom_labels: dict mapping scenario suffix to label.
    show_rittal: bool, whether to show Rittal (Pem) values or not.
    """
    n_levels = 0
    for env_data in data_by_env.values():
        numeric_items = [(k, v) for k, v in env_data.items() if is_load_key(k)]
        n_levels = max(n_levels, len(numeric_items))
    if n_levels == 0:
        print("No load level data to plot.")
        return

    # Detect whether any environment contains per-container (RS2/RS3) data –
    # those panels hold more boxes and need a wider figure.
    any_split = any(
        _scenario_dict_has_split(env_data.get(k, {}))
        for env_data in data_by_env.values()
        for k in env_data if is_load_key(k)
    )

    fig_width = min(22, (4.5 if any_split else 3.5) * n_levels)
    fig_height = 9
    fig, axes = plt.subplots(1, n_levels, figsize=(fig_width, fig_height), sharey=True)
    if n_levels == 1:
        axes = [axes]

    # Collect all sorted load levels across all environments
    all_load_levels = set()
    for env_data in data_by_env.values():
        all_load_levels.update(k for k in env_data if is_load_key(k))
    sorted_loads = sorted(all_load_levels, key=parse_load_key)

    # The per-environment figures are plotted one environment at a time; an
    # environment prefix on every x-tick label only makes the labels
    # unreadable, so it is only used when multiple environments are mixed.
    single_env = len(data_by_env) == 1

    legend_needed = False
    for ax, load_level in zip(axes, sorted_loads):
        # Each group holds one x-tick label and one (full-width) or two
        # (half-width, container 1 + 2) boxes.
        groups = []  # [{'label': str, 'boxes': [(series, facecolor, hatch)]}]
        split_panel = False
        for env_name, env_data in data_by_env.items():
            scenario_dict = env_data.get(load_level, {})
            if not scenario_dict:
                continue
            env_prefix = f'{env_name}: ' if env_name and env_name != 'flat' and not single_env else ''
            has_split = _scenario_dict_has_split(scenario_dict)
            split_panel = split_panel or has_split
            if has_split:
                # RS2/RS3: single P_S reference box aggregated over the
                # powercap measurements of all tool scenarios.
                all_powercap = [s for sdata in scenario_dict.values() for _rl, s in sdata.get('powercap', [])]
                if all_powercap:
                    combined = pd.concat(all_powercap, ignore_index=True)
                    groups.append({
                        'label': f'{env_prefix}$P_{{S}}$',
                        'boxes': [(combined, TOOL_COLORS['powercap'], '')],
                    })
            for suffix in (scenario_suffixes if scenario_suffixes is not None else scenario_dict.keys()):
                matching = [k for k in scenario_dict.keys() if scenario_endswith(k, suffix)] if scenario_suffixes else [suffix]
                label = custom_labels.get(suffix, suffix) if custom_labels else suffix
                for kind, math_label, color in _TOOL_PLOT_ORDER:
                    if kind == 'rittal' and not show_rittal:
                        continue
                    if kind == 'powercap' and has_split:
                        continue  # already added as single P_S reference box
                    plain = []
                    per_container = {1: [], 2: []}
                    for scenario in matching:
                        sdata = scenario_dict.get(scenario, {})
                        plain.extend(s for _rl, s in sdata.get(kind, []))
                        per_container[1].extend(s for _rl, s in sdata.get(f'{kind}_c1', []))
                        per_container[2].extend(s for _rl, s in sdata.get(f'{kind}_c2', []))
                    if plain:
                        combined = pd.concat(plain, ignore_index=True)
                        facecolor = color if has_split else 'lightgrey'
                        groups.append({
                            'label': f'{env_prefix}{label}{math_label}',
                            'boxes': [(combined, facecolor, '')],
                        })
                    if per_container[1] or per_container[2]:
                        boxes = []
                        for container_idx, hatch in ((1, ''), (2, CONTAINER2_HATCH)):
                            if per_container[container_idx]:
                                combined = pd.concat(per_container[container_idx], ignore_index=True)
                                boxes.append((combined, color, hatch))
                        groups.append({
                            'label': f'{env_prefix}{label}{math_label}',
                            'boxes': boxes,
                        })
        if groups:
            tick_positions = []
            tick_labels = []
            for pos, group in enumerate(groups, start=1):
                boxes = group['boxes']
                if len(boxes) == 1:
                    offsets = [0.0]
                    width = 0.5
                else:
                    # Two containers side by side, each half as wide
                    offsets = [-0.15, 0.15]
                    width = 0.25
                for (series, facecolor, hatch), offset in zip(boxes, offsets):
                    bp = ax.boxplot(
                        [series],
                        positions=[pos + offset],
                        widths=width,
                        patch_artist=True,
                        medianprops=dict(color="grey", linewidth=2.5),
                        showmeans=True,
                        meanprops={"marker": "x", "markerfacecolor": "red", "markeredgecolor": "red", "markersize": 10},
                    )
                    patch = bp['boxes'][0]
                    patch.set_facecolor(facecolor)
                    if hatch:
                        patch.set_hatch(hatch)
                tick_positions.append(pos)
                tick_labels.append(group['label'])
            ax.set_xticks(tick_positions)
            label_fontsize = 18 if len(groups) <= 7 else 14
            ax.set_xticklabels(tick_labels, rotation=0, ha='right', fontsize=label_fontsize)
            ax.set_xlim(0.5, len(groups) + 0.5)
        load_num, variant = parse_load_key(load_level)
        title = f'Load: {load_num * 3} T/s'
        if split_panel:
            title += f' ({distribution_label(variant)})'
            legend_needed = True
        ax.set_title(title, fontsize=22)
        ax.set_xlabel('', fontsize=18)
        ax.tick_params(axis='y', labelsize=16)
        ax.tick_params(axis='x', labelsize=16)
        ax.yaxis.grid(True, linestyle='--', alpha=0.7)
    axes[0].set_ylabel('Power (Watts)', fontsize=20)
    plt.tight_layout()
    plt.subplots_adjust(bottom=0.18, top=0.90, left=0.07, right=0.98)
    if legend_needed:
        legend_handles = [
            Patch(facecolor='white', edgecolor='black', label='Container 1'),
            Patch(facecolor='white', edgecolor='black', hatch=CONTAINER2_HATCH, label='Container 2'),
        ]
        fig.legend(handles=legend_handles, loc='lower center', ncol=2, fontsize=16, frameon=False,
                   bbox_to_anchor=(0.5, 0.0))
    plt.savefig(output_path, format='pdf', bbox_inches='tight')
    plt.close()
    print(f"Saved combined boxplot to {output_path}")


def format_env_label(env_key):
    """Map an environment directory name to the table label.

    ``RS_2`` -> ``Container (RS2)``, ``Container`` -> ``Container (RS1)``,
    ``VM`` -> ``VM (RS1)``.
    """
    m = re.match(r"^rs_?(\d+)$", env_key, flags=re.IGNORECASE)
    if m:
        return f"Container (RS{m.group(1)})"
    if "(RS" in env_key:
        return env_key
    return f"{env_key} (RS1)"


# Tool kinds evaluated per-container in the RS2/RS3 numeric table: (data
# key, display name).
_RS_TOOL_KINDS = [
    ("kepler", "Kepler"),
    ("scaphandre", "Scaphandre"),
    ("powerapi", "PowerAPI"),
    ("otjae", "OTJAE"),
    ("joularjx", "JoularJX"),
    ("joularjx_procfs", "JoularJX (procfs)"),
]


def _rs_container_run_means(scenario_dict, tool_key):
    """Per-run mean power for container 1 and container 2 of *tool_key*,
    pooled across every scenario in *scenario_dict* whose name matches
    *tool_key* (mirroring the boxplot function's matching logic).

    ``joularjx_procfs`` (the procfs-CPU-utilization variant of JoularJX,
    P_P_JM) is a special case: it is stored as extra ``joularjx_procfs_c1``/
    ``_c2`` keys *inside* the ``joularjx`` scenario's data, not under its own
    scenario name, so the scenario-name match must use ``joularjx`` while the
    data-key lookup still uses ``joularjx_procfs`` -- exactly as
    plot_all_load_levels_in_one_row already does (see its ``kind`` vs
    ``suffix`` split). Using ``tool_key`` for both, as before, silently
    matched no scenario at all for this tool.

    Returns ``({run_label: mean_c1}, {run_label: mean_c2})``.
    """
    match_key = 'joularjx' if tool_key == 'joularjx_procfs' else tool_key
    c1_by_run = defaultdict(list)
    c2_by_run = defaultdict(list)
    for scenario, sdata in scenario_dict.items():
        if not scenario_endswith(scenario, match_key):
            continue
        for run_label, series in sdata.get(f'{tool_key}_c1', []):
            c1_by_run[run_label].append(series)
        for run_label, series in sdata.get(f'{tool_key}_c2', []):
            c2_by_run[run_label].append(series)
    c1_means = {r: float(pd.concat(v, ignore_index=True).mean()) for r, v in c1_by_run.items() if v}
    c2_means = {r: float(pd.concat(v, ignore_index=True).mean()) for r, v in c2_by_run.items() if v}
    return c1_means, c2_means


def print_rs_load_distribution_table(env_data_store):
    """Print a LaTeX table of per-container power (mean$\\pm$std across n=3
    repetitions) and each tool's measured load-distribution split for RS2/RS3,
    plus a statistical note per tool on whether the measured split
    significantly deviates from the JMeter-configured target split.

    This is the numeric counterpart to the RS2/RS3 boxplots: the paper's
    Discussion argues that JoularJX's JMX-based CPU utilization source is
    unaware of RS2's per-container CPU-socket pinning, causing it to
    attribute an almost equal (~50/50) power share regardless of the actual
    configured load imbalance, while other tools correctly track the split.
    The measured-vs-target deviation, tested here via a paired Cohen's dz and
    an exact Wilcoxon signed-rank test (session-paired across all load
    levels/distribution variants within an environment), quantifies that
    claim instead of leaving it purely qualitative.
    """
    print("\n=== RS2/RS3 Load-Distribution LaTeX Table ===")
    print("\\begin{table*}[!htbp]")
    print("    \\centering")
    print("    \\caption{Mean per-container power and measured load-distribution split by tool (RS2/RS3); cells show mean$\\pm$std across n=3 repetitions}")
    print("    \\label{tab:rs2_rs3_load_distribution}")
    print("    \\resizebox{\\textwidth}{!}{%")
    print("        \\begin{tabular}{ |c|c|c|c|c|c|c| }")
    print("            \\hline")
    print("            Environment & Load & Target Split (C1/C2) & Tool & P\\textsubscript{C1} & P\\textsubscript{C2} & Measured Split C1 \\\\")
    print("            \\hline")

    # Session-paired (measured C1 split % - target C1 split %) across all
    # load levels/distribution variants, per environment per tool -- for the
    # statistical notes below the table.
    session_deviation_by_tool = defaultdict(lambda: defaultdict(list))

    for env_key, data_by_load in env_data_store.items():
        numeric_items = [(k, v) for k, v in data_by_load.items() if is_load_key(k)]
        sorted_loads = sorted(numeric_items, key=lambda x: parse_load_key(x[0]))
        env_label = format_env_label(env_key)
        if not sorted_loads:
            continue
        for load_level, scenario_dict in sorted_loads:
            load_num, variant = parse_load_key(load_level)
            target_label = distribution_label(variant)
            m = _DISTRIBUTION_VARIANT_RE.match(variant)
            target_c1_pct = float(m.group(1)) if m else 50.0
            load_label = f"{load_num * 3}T/s"

            for tool_key, tool_name in _RS_TOOL_KINDS:
                c1_means, c2_means = _rs_container_run_means(scenario_dict, tool_key)
                common_runs = sorted(set(c1_means) & set(c2_means))
                if not common_runs:
                    continue
                c1_stats = summarize_repetitions([c1_means[r] for r in common_runs])
                c2_stats = summarize_repetitions([c2_means[r] for r in common_runs])
                split_pcts = [
                    c1_means[r] / (c1_means[r] + c2_means[r]) * 100
                    for r in common_runs
                    if (c1_means[r] + c2_means[r]) != 0
                ]
                split_stats = summarize_repetitions(split_pcts)
                session_deviation_by_tool[env_key][tool_name].extend(p - target_c1_pct for p in split_pcts)

                c1_cell = fmt_mean_std(c1_stats["mean"], c1_stats["std"])
                c2_cell = fmt_mean_std(c2_stats["mean"], c2_stats["std"])
                split_cell = fmt_mean_std(split_stats["mean"], split_stats["std"], unit="\\%")
                print(f"            {env_label} & {load_label} & {target_label} & {tool_name} & {c1_cell} & {c2_cell} & {split_cell} \\\\")
        print("            \\hline")

    print("        \\end{tabular}")
    print("    }")
    print("\\end{table*}")

    # --- Statistical notes: measured C1 split % vs target C1 split %, session-paired ---
    for env_key, tool_devs in session_deviation_by_tool.items():
        for tool_name, deviations in tool_devs.items():
            if not deviations:
                continue
            d_paired = cohens_d_paired(deviations)
            wilcoxon = wilcoxon_signed_rank_exact(deviations)
            print(f"% Statistical note ({env_key}, {tool_name} measured C1 split % vs target C1 split %), session-paired (n={wilcoxon['n_nonzero']}):")
            if d_paired is not None:
                print(f"%   Cohen's dz = {d_paired:.3f}")
            if wilcoxon["p_two_sided"] is not None:
                print(f"%   Wilcoxon signed-rank: W+={wilcoxon['statistic']:.1f}, p={wilcoxon['p_two_sided']:.4f} (p_floor={wilcoxon['p_floor']:.4f})")


def main():
    """
    Main entry point: collects data, generates and saves the boxplot PDF for all load levels.
    Handles results per environment (Container, VM, RS_2, RS_3, etc.) separately.
    """
    trim_seconds = 60
    scenario_suffixes = [
        "tools",
        "kepler",
        "scaphandre",
        "powerapi",
        "otjae",
        "joularjx"
    ]
    custom_labels = {
        "idle_no_tools": "Idle",
        "tools": "",
        "none": "Container\nIdle (CI)",
        "idle": "CI, Powercap (PC)\nand ProcFS",
        "kepler": "",
        "scaphandre": "",
        "powerapi": "",
        "otjae": "",
        "joularjx": ""
    }
    included_load_levels = ["230", "350", "480", "560"]
    show_rittal = False

    # This script only plots RS2/RS3 (see module purpose); restricting
    # collection to those environments skips the much larger Container/VM
    # (RS1) per-scenario data collection entirely, which dominates runtime.
    env_data_store = collect_data_by_load_level(
        trim_seconds=trim_seconds,
        scenario_suffixes=scenario_suffixes,
        included_load_levels=included_load_levels,
        environments=["RS2", "RS3"],
    )

    # --- Plot per environment ---
    for env_key, data_by_load in env_data_store.items():
        numeric_items = [(k, v) for k, v in data_by_load.items() if is_load_key(k)]
        env_output = f"./process_power_consumption_boxplots_{env_key}_all_loads.pdf"
        plot_all_load_levels_in_one_row({env_key: data_by_load}, env_output, scenario_suffixes=scenario_suffixes, custom_labels=custom_labels, show_rittal=show_rittal)
        print(f"Environment {env_key}: Saved plot to {env_output}")
        print(f"Environment {env_key}: Processed {len(numeric_items)} load levels")

    # --- Numeric load-distribution table ---
    print_rs_load_distribution_table(env_data_store)


# Run the script if executed directly
if __name__ == "__main__":
    main()
