"""
visualizeLoadLevelProcessPowerConsumptionAsBoxplots.py

Estimate per-process power consumption and generate boxplots grouped by load level. This script contains helpers to parse procfs, powercap and other experiment logs and uses attribution models to distribute system-level power to processes (e.g., using procfs CPU fractions) and to add memory/storage power contributions.

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
    calculate_power_from_energy,
    load_power_data,
    get_jmeter_time_bounds,
    extract_service_pids,
    parse_scaphandre_http_logger,
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
    "otjae": "#cab2d6",
}

# Hatch pattern distinguishing container 2 boxes from container 1 boxes.
CONTAINER2_HATCH = "//"

# Mapping of run-dir variants to the load distribution between the two
# containers (container 1 / container 2). ``c2-33`` means container 2
# receives 33% of the load, i.e. a 67/33 distribution.
_DISTRIBUTION_LABELS = {
    "c2-33": "67/33",
    "c2-67": "33/67",
}


def is_split_scenario(scenario_name: str) -> bool:
    """RS2/RS3 scenarios run two containers and are evaluated per container."""
    return bool(re.search(r"_rs[23]$", scenario_name, flags=re.IGNORECASE))


def strip_run_suffix(run_dir_name: str) -> str:
    """Strip a trailing run-repetition suffix from a run directory name.

    ``350_run2`` -> ``350``, ``480_c2-33_1`` -> ``480_c2-33``, ``560`` -> ``560``.
    """
    return re.sub(r"_(?:run)?\d+$", "", run_dir_name)


def regroup_by_variant(load_level_map: dict) -> dict:
    """Regroup ``{load_level: [run_dir, …]}`` so that load-distribution
    variants (e.g. ``480_c2-33`` vs ``480_c2-67``) become separate keys
    instead of being merged under their numeric prefix.
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


def distribution_label(variant: str) -> str:
    """Return the container 1 / container 2 load distribution for a variant."""
    return _DISTRIBUTION_LABELS.get(variant, "50/50")


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
    Returns a DataFrame with columns: ['datetime', 'Power']
    """
    if split_containers:
        if len(service_pids) != 2:
            raise ValueError(
                f"Expected exactly 2 service PIDs but got {len(service_pids)}"
            )
        return [
            parse_kepler_http_logger(
                file_path,
                [service_pids[0]],
                trim_seconds,
                jmeter_bounds,
                split_containers=False
            ),
            parse_kepler_http_logger(
                file_path,
                [service_pids[1]],
                trim_seconds,
                jmeter_bounds,
                split_containers=False
            )
        ]
    import re
    data = []
    current_timestamp = None
    # Regex for DATA line and kepler metric line
    data_line_re = re.compile(r"^DATA:.* at (\d+)")
    kepler_line_re = re.compile(r'kepler_process_cpu_watts\{[^}]*pid="(\d+)"[^}]*\} ([\d\.eE+-]+)')
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
                    if pid in service_pids:
                        dt = pd.to_datetime(current_timestamp, unit='ms')
                        data.append({'datetime': dt, 'Power': float(value)})
    df = pd.DataFrame(data)
    # Optionally trim the time series
    if not df.empty and (trim_seconds > 0 or jmeter_bounds is not None):
        df = trim_time_series(df, trim_seconds, jmeter_bounds)
    return df

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
    #print(f"\n[DEBUG] --- parse_procfs_joularjx ---")
    #print(f"procfs_file: {procfs_file}")
    #print(f"powercap_files: {powercap_files}")
    #print(f"service_pids: {service_pids}")
    """
    Parses procfs CSV and powercap CSVs, computes process-specific power consumption over time.
    Returns a DataFrame with columns: ['datetime', 'Power'] for the process.
    """
    import pandas as pd
    if split_containers:
        if len(service_pids) != 2:
            raise ValueError(
                f"Expected exactly 2 service PIDs but got {len(service_pids)}"
            )

        return [
            parse_procfs_joularjx(
                procfs_file,
                powercap_files,
                [service_pids[0]],
                trim_seconds,
                jmeter_bounds,
                n_cores,
                ticks_per_sec,
                split_containers=False,
            ),
            parse_procfs_joularjx(
                procfs_file,
                powercap_files,
                [service_pids[1]],
                trim_seconds,
                jmeter_bounds,
                n_cores,
                ticks_per_sec,
                split_containers=False,
            ),
        ]

    # Factor out procfs parsing to reusable function
    proc_util, sys_df, _, _ = parse_procfs_data(procfs_file, service_pids, n_cores=n_cores, ticks_per_sec=ticks_per_sec, jmeter_bounds=jmeter_bounds)
    if proc_util is None or sys_df is None:
        return pd.DataFrame(columns=['datetime', 'Power'])
    # Read and sum powercap power for both sockets at each timestamp
    powercap_power_list = []
    for f in powercap_files:
        try:
            df_power = pd.read_csv(f)
            df_power = calculate_power_from_energy(df_power)
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
    #print(f"powercap_df shape: {powercap_df.shape}")
    # Harmonize timestamps to seconds relative to steady-state start
    if jmeter_bounds is not None and jmeter_bounds[0] is not None:
        steady_state_start = jmeter_bounds[0]
    else:
        # fallback: use min timestamp in proc_util
        steady_state_start = proc_util['datetime'].min() if not proc_util.empty else None
    if steady_state_start is None:
        print("No steady-state start time found for harmonization.")
        return pd.DataFrame(columns=['datetime', 'Power'])
    # Add rel_sec column (integer seconds since steady-state start)
    proc_util = proc_util.copy()
    proc_util['rel_sec'] = (proc_util['datetime'] - steady_state_start).dt.total_seconds().astype(int)
    proc_util = proc_util[proc_util['rel_sec'] >= 0]
    #print(f"proc_util rel_sec min: {proc_util['rel_sec'].min() if not proc_util.empty else 'empty'}, max: {proc_util['rel_sec'].max() if not proc_util.empty else 'empty'}")
    powercap_df = powercap_df.copy()
    powercap_df['rel_sec'] = (powercap_df['datetime'] - steady_state_start).dt.total_seconds().astype(int)
    powercap_df = powercap_df[powercap_df['rel_sec'] >= 0]
    #print(f"powercap_df rel_sec min: {powercap_df['rel_sec'].min() if not powercap_df.empty else 'empty'}, max: {powercap_df['rel_sec'].max() if not powercap_df.empty else 'empty'}")
    # Merge on rel_sec (exact match)
    merged = pd.merge(proc_util, powercap_df, on='rel_sec', how='inner', suffixes=('_proc', '_powercap'))
    #print(f"merged shape: {merged.shape}")
    merged['Power'] = merged['util_ratio'] * merged['Power']
    # Use the datetime from proc_util for output
    result = merged[['datetime_proc', 'Power']].rename(columns={'datetime_proc': 'datetime'}).dropna()
    # Optionally trim
    if not result.empty and (trim_seconds > 0 or jmeter_bounds is not None):
        result = trim_time_series(result, trim_seconds, jmeter_bounds)
    return result


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

def collect_data_by_load_level(trim_seconds=0, scenario_suffixes=None, included_load_levels=None):
    """
    Collects and groups all Rittal and Powercap data by environment, load level (numeric prefix of directory name).
    Aggregates all runs (e.g., 350, 350_run2, 350_run3) for each load level within each environment.
    Load-distribution variants of RS2/RS3 (e.g. 480_c2-33 vs 480_c2-67) are kept as separate load-level keys.
    Only includes scenario subdirectories matching scenario_suffixes if provided.
    If included_load_levels is provided (list of strings), only load levels whose numeric prefix is in the
    list are included in the returned data (except for pcpumin/pcpumax, which always use all data).
    For RS2/RS3 scenarios (two containers), per-container results are stored under
    '<tool>_c1' / '<tool>_c2' keys instead of '<tool>'.
    Returns a dict: {environment: {load_level: {scenario: {'rittal': [series...], 'powercap': [series...]}, ...}, ...}}
    """
    exp_results = Path(__file__).resolve().parent
    env_names = discover_environments(exp_results)
    if not env_names:
        env_names = [None]

    # Outer structure: data_by_env[environment][load_level][scenario] = {rittal: [...], ...}
    data_by_env = {}

    # RS2/RS3 have no uninstrumented "tools" baseline, so pcpumin/pcpumax
    # cannot be derived there. Fall back to the values of the first
    # environment that provides them (same host hardware).
    fallback_pcpumin = None
    fallback_pcpumax = None

    for env_name in env_names:
        load_level_map = build_run_dirs(exp_results, environment=env_name) if env_name else build_run_dirs(exp_results)
        # Keep RS2/RS3 load-distribution variants separate (480_c2-33 vs 480_c2-67)
        load_level_map = regroup_by_variant(load_level_map)
        data_by_load = defaultdict(lambda: defaultdict(lambda: defaultdict(list)))

        label = f"[{env_name}] " if env_name else ""

        # --- Step 1: Process docker_tools scenario first and collect mean powercap for 0 and 560 ---
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
        data_by_load['pcpumin'] = pcpumin
        data_by_load['pcpumax'] = pcpumax
        print(f"{label}Min Mean CPU Power: {pcpumin} W, Max Mean CPU Power: {pcpumax} W")

        # --- Step 2: Process all scenarios as before ---
        for load_level, dirs in load_level_map.items():
            # If included_load_levels is set, skip load levels not in the list (except for pcpumin/pcpumax)
            if included_load_levels is not None and load_level.split("_")[0] not in included_load_levels:
                continue
            for run_path in dirs:
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
                                    sdata['powercap'].append(power_data['Power'])
                            except Exception as e:
                                print(f"Error loading {file_path}: {e}")
                    # Load and append joularjx process power
                    if scenario_matches(scenario_name, 'joularjx'):
                        procfs_files = list(scenario_dir.glob('**/procfs_*_joularjx_*.csv'))
                        powercap_files = list(scenario_dir.glob('**/powercap_*.csv'))
                        experiment_log_path = scenario_dir / 'logs' / 'experiment_log.jsonl'
                        service_pids = extract_service_pids(str(experiment_log_path)) if experiment_log_path.exists() else []
                        if procfs_files and powercap_files and service_pids:
                            try:
                                joularjx_data = parse_procfs_joularjx(
                                    str(procfs_files[0]),
                                    [str(f) for f in powercap_files],
                                    service_pids,
                                    trim_seconds=trim_seconds,
                                    jmeter_bounds=jmeter_bounds,
                                    split_containers=split,
                                )

                                if split:
                                    for idx, container_df in enumerate(joularjx_data, start=1):
                                        if container_df is not None and not container_df.empty:
                                            sdata[f'joularjx_c{idx}'].append(container_df['Power'])
                                else:
                                    if not joularjx_data.empty:
                                        sdata['joularjx'].append(joularjx_data['Power'])
                            except Exception as e:
                                print(f"Error loading joularjx procfs/powercap: {e}")
                    if scenario_matches(scenario_name, 'tools'):
                        rittal_files = list(scenario_dir.glob('**/rittal_*.csv'))
                        powercap_files = list(scenario_dir.glob('**/powercap_*.csv'))
                        for file_path in rittal_files:
                            try:
                                power_data = load_rittal_data(str(file_path), trim_seconds, jmeter_bounds)
                                if 'Power (Watts)' in power_data.columns:
                                    sdata['rittal'].append(power_data['Power (Watts)'])
                            except Exception as e:
                                print(f"Error loading {file_path}: {e}")
                        for file_path in powercap_files:
                            try:
                                power_data = load_power_data(str(file_path), trim_seconds, jmeter_bounds)
                                if 'Power' in power_data.columns:
                                    sdata['powercap'].append(power_data['Power'])
                            except Exception as e:
                                print(f"Error loading {file_path}: {e}")
                    kepler_files = list(scenario_dir.glob('**/http_logger_spring_docker_kepler*.csv')) + \
                                   list(scenario_dir.glob('**/http_logger_spring_vm_kepler*.csv'))
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
                                            sdata[f'kepler_c{idx}'].append(container_df['Power'])
                                else:
                                    if not kepler_data.empty:
                                        sdata['kepler'].append(kepler_data['Power'])
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
                                            sdata[f'scaphandre_c{idx}'].append(container_df['Power'])
                                else:
                                    if not scaphandre_data.empty:
                                        sdata['scaphandre'].append(scaphandre_data['Power'])
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
                                        sdata['powerapi_c1'].append(powerapi_data['Power'])
                                    else:
                                        sdata['powerapi'].append(powerapi_data['Power'])
                            except Exception as e:
                                print(f"Error loading powerapi file {file_path}: {e}")
                        for file_path in powerapi_c2_files:
                            try:
                                powerapi_data = parse_powerapi_report(str(file_path), trim_seconds, jmeter_bounds)
                                if not powerapi_data.empty:
                                    if split:
                                        sdata['powerapi_c2'].append(powerapi_data['Power'])
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
                                            sdata[f'otjae_c{idx}'].append(container_series)
                                else:
                                    if otjae_power is not None and not otjae_power.empty:
                                        sdata['otjae'].append(otjae_power)
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
                all_powercap = [s for sdata in scenario_dict.values() for s in sdata.get('powercap', [])]
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
                        plain.extend(sdata.get(kind, []))
                        per_container[1].extend(sdata.get(f'{kind}_c1', []))
                        per_container[2].extend(sdata.get(f'{kind}_c2', []))
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

    # Collect all environments (Container, VM, RS_2, RS_3, ...) in one pass.
    env_data_store = collect_data_by_load_level(
        trim_seconds=trim_seconds,
        scenario_suffixes=scenario_suffixes,
        included_load_levels=included_load_levels,
    )

    # --- Plot per environment ---
    for env_key, data_by_load in env_data_store.items():
        numeric_items = [(k, v) for k, v in data_by_load.items() if is_load_key(k)]
        env_output = f"./process_power_consumption_boxplots_{env_key}_all_loads.pdf"
        plot_all_load_levels_in_one_row({env_key: data_by_load}, env_output, scenario_suffixes=scenario_suffixes, custom_labels=custom_labels, show_rittal=show_rittal)
        print(f"Environment {env_key}: Saved plot to {env_output}")
        print(f"Environment {env_key}: Processed {len(numeric_items)} load levels")


# Run the script if executed directly
if __name__ == "__main__":
    main()
