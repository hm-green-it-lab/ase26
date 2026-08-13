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

import pandas as pd
import numpy as np
import matplotlib.pyplot as plt
from pathlib import Path
from collections import defaultdict

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
    filter_application_powerapi_files,
    discover_environments,
    scenario_matches,
    scenario_matches_any,
    summarize_repetitions,
    cohens_d_paired,
    wilcoxon_signed_rank_exact,
    fmt_mean_std,
    per_run_means,
)


def parse_kepler_http_logger(file_path, service_pids, trim_seconds=0, jmeter_bounds=None):
    """
    Parses a large http_logger_spring_docker_kepler file, extracts kepler_process_cpu_watts for the given service_pids.
    Only includes values within the jmeter_bounds timeframe if provided.
    Returns a DataFrame with columns: ['datetime', 'Power']
    """
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


def process_docker_otjae(scenario_dir, trim_seconds, pcpumin, pcpumax):
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
    # Parse procfs data (get system utilization)
    proc_util, sys_df, mem_deltas_df, io_deltas_df = parse_procfs_data(procfs_file, service_pids, jmeter_bounds=jmeter_bounds)
    if sys_df is None or sys_df.empty:
        return None
    # Calculate system CPU utilization per second
    sys_df = sys_df.copy()
    sys_df['sys_util'] = sys_df['delta_cpu'] / (sys_df['interval'])
    # Normalize to [0,1] by dividing by n_cores (assume 80 as default)
    n_cores = 80
    sys_df['sys_util_norm'] = sys_df['sys_util'] / n_cores
    sys_df['sys_util_norm'] = sys_df['sys_util_norm'].clip(lower=0, upper=1)
    # Calculate power: P = pcpumin + (sys_util_norm * (pcpumax - pcpumin))
    sys_df['Power'] = pcpumin + (sys_df['sys_util_norm'] * (pcpumax - pcpumin))


    # Add memory power (VmRSS in kB to GB, then * MEMORY_POWER_W_PER_GB)
    if mem_deltas_df is not None and not mem_deltas_df.empty:
        mem_group = mem_deltas_df.groupby('datetime')['VmRSS'].sum().reset_index()
        mem_group['VmRSS_GB'] = mem_group['VmRSS'] / (1024 * 1024)
        mem_group['Pmemory'] = mem_group['VmRSS_GB'] * MEMORY_POWER_W_PER_GB
        sys_df = pd.merge_asof(sys_df.sort_values('datetime'), mem_group[['datetime', 'Pmemory']].sort_values('datetime'), on='datetime', direction='nearest', tolerance=pd.Timedelta('1s'))
        sys_df['Pmemory'] = sys_df['Pmemory'].fillna(0)
        sys_df['Power'] = sys_df['Power'] + sys_df['Pmemory']

    # Add storage power (delta_read_bytes + delta_write_bytes in TB * STORAGE_POWER_W_PER_TB)
    if io_deltas_df is not None and not io_deltas_df.empty:
        # For each timestamp, sum deltas across all pids
        io_group = io_deltas_df.groupby('datetime')[['delta_read_bytes', 'delta_write_bytes']].sum().reset_index()
        # Convert bytes to TB
        io_group['total_bytes'] = io_group['delta_read_bytes'].fillna(0) + io_group['delta_write_bytes'].fillna(0)
        io_group['total_TB'] = io_group['total_bytes'] / (1024 ** 4)
        io_group['Pstorage'] = io_group['total_TB'] * STORAGE_POWER_W_PER_TB
        # Merge storage power into sys_df by datetime (nearest)
        sys_df = pd.merge_asof(sys_df.sort_values('datetime'), io_group[['datetime', 'Pstorage']].sort_values('datetime'), on='datetime', direction='nearest', tolerance=pd.Timedelta('1s'))
        sys_df['Pstorage'] = sys_df['Pstorage'].fillna(0)
        sys_df['Power'] = sys_df['Power'] + sys_df['Pstorage']

    # We do not add network power as we do not have per-process network I/O data

    # Return as a Series for boxplot
    return sys_df[['Power']].dropna()['Power']

def collect_data_by_load_level(trim_seconds=0, scenario_suffixes=None, included_load_levels=None):
    """
    Collects and groups all Rittal and Powercap data by environment, load level (numeric prefix of directory name).
    Aggregates all runs (e.g., 350, 350_run2, 350_run3) for each load level within each environment.
    Only includes scenario subdirectories matching scenario_suffixes if provided.
    If included_load_levels is provided (list of strings), only those load levels are included in the returned data (except for pcpumin/pcpumax, which always use all data).
    Returns a dict: {environment: {load_level: {scenario: {'rittal': [series...], 'powercap': [series...]}, ...}, ...}}
    """
    exp_results = Path(__file__).resolve().parent
    env_names = [e for e in discover_environments(exp_results) if e in ("Container", "VM")]
    if not env_names:
        env_names = [None]

    # Outer structure: data_by_env[environment][load_level][scenario] = {rittal: [...], ...}
    data_by_env = {}

    for env_name in env_names:
        load_level_map = build_run_dirs(exp_results, environment=env_name) if env_name else build_run_dirs(exp_results)
        data_by_load = defaultdict(lambda: defaultdict(lambda: {'rittal': [], 'powercap': [], 'kepler': [], 'scaphandre': [], 'powerapi': [], 'joularjx': [], 'otjae': []}))

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
        data_by_load['pcpumin'] = pcpumin
        data_by_load['pcpumax'] = pcpumax
        print(f"{label}Min Mean CPU Power: {pcpumin} W, Max Mean CPU Power: {pcpumax} W")

        # --- Step 2: Process all scenarios as before ---
        for load_level, dirs in load_level_map.items():
            # If included_load_levels is set, skip load levels not in the list (except for pcpumin/pcpumax)
            if included_load_levels is not None and load_level not in included_load_levels:
                continue
            for run_path in dirs:
                for scenario_dir in run_path.iterdir():
                    if not scenario_dir.is_dir():
                        continue
                    scenario_name = scenario_dir.name
                    if scenario_suffixes is not None and not scenario_matches_any(scenario_name, scenario_suffixes):
                        continue
                    jmeter_bounds = get_jmeter_time_bounds(str(scenario_dir), trim_seconds)
                    # Load and append joularjx process power, computed directly from
                    # JoularJX's own per-second "all methods" power output rather
                    # than estimated from procfs CPU share and powercap.
                    if scenario_matches(scenario_name, 'joularjx'):
                        try:
                            joularjx_data = parse_joularjx_power(scenario_dir, trim_seconds=trim_seconds, jmeter_bounds=jmeter_bounds)
                            if not joularjx_data.empty:
                                data_by_load[load_level][scenario_name]['joularjx'].append(joularjx_data['Power'])
                        except Exception as e:
                            print(f"Error loading joularjx result archive: {e}")
                    if scenario_matches(scenario_name, 'tools'):
                        rittal_files = list(scenario_dir.glob('**/rittal_*.csv'))
                        powercap_files = list(scenario_dir.glob('**/powercap_*.csv'))
                        for file_path in rittal_files:
                            try:
                                power_data = load_rittal_data(str(file_path), trim_seconds, jmeter_bounds)
                                if 'Power (Watts)' in power_data.columns:
                                    data_by_load[load_level][scenario_name]['rittal'].append(power_data['Power (Watts)'])
                            except Exception as e:
                                print(f"Error loading {file_path}: {e}")
                        for file_path in powercap_files:
                            try:
                                power_data = load_power_data(str(file_path), trim_seconds, jmeter_bounds)
                                if 'Power' in power_data.columns:
                                    data_by_load[load_level][scenario_name]['powercap'].append(power_data['Power'])
                            except Exception as e:
                                print(f"Error loading {file_path}: {e}")
                    kepler_files = list(scenario_dir.glob('**/http_logger_spring_docker_kepler*.csv'))
                    if kepler_files:
                        experiment_log_path = scenario_dir / 'logs' / 'experiment_log.jsonl'
                        service_pids = extract_service_pids(str(experiment_log_path)) if experiment_log_path.exists() else []
                        for file_path in kepler_files:
                            try:
                                kepler_data = parse_kepler_http_logger(str(file_path), service_pids, trim_seconds, jmeter_bounds)
                                if not kepler_data.empty:
                                    data_by_load[load_level][scenario_name]['kepler'].append(kepler_data['Power'])
                            except Exception as e:
                                print(f"Error loading {file_path}: {e}")
                    scaphandre_files = list(scenario_dir.glob('**/http_logger_spring_docker_scaphandre*.csv')) + \
                                       list(scenario_dir.glob('**/http_logger_spring_vm_scaphandre*.csv'))
                    if scaphandre_files:
                        experiment_log_path = scenario_dir / 'logs' / 'experiment_log.jsonl'
                        service_pids = extract_service_pids(str(experiment_log_path)) if experiment_log_path.exists() else []
                        for file_path in scaphandre_files:
                            try:
                                scaphandre_data = parse_scaphandre_http_logger(str(file_path), service_pids, trim_seconds, jmeter_bounds)
                                if not scaphandre_data.empty:
                                    data_by_load[load_level][scenario_name]['scaphandre'].append(scaphandre_data['Power'])
                            except Exception as e:
                                print(f"Error loading {file_path}: {e}")
                    if scenario_matches(scenario_name, 'powerapi'):
                        powerapi_files = list(scenario_dir.glob('**/smartwattsresults/localhost-/energy.slice/**/docker-*.scope/PowerReport.csv')) + \
                                         list(scenario_dir.glob('**/smartwattsresults/localhost-/energy.slice/**/vm-*.scope/PowerReport.csv'))
                        powerapi_files = filter_application_powerapi_files(powerapi_files)
                        for file_path in powerapi_files:
                            try:
                                powerapi_data = parse_powerapi_report(str(file_path), trim_seconds, jmeter_bounds)
                                if not powerapi_data.empty:
                                    data_by_load[load_level][scenario_name]['powerapi'].append(powerapi_data['Power'])
                            except Exception as e:
                                print(f"Error loading {file_path}: {e}")
                    if scenario_matches(scenario_name, 'otjae'):
                        pcpumin_ev = data_by_load.get('pcpumin', None)
                        pcpumax_ev = data_by_load.get('pcpumax', None)
                        if pcpumin_ev is not None and pcpumax_ev is not None:
                            try:
                                otjae_power = process_docker_otjae(scenario_dir, trim_seconds, pcpumin_ev, pcpumax_ev)
                                if otjae_power is not None and not otjae_power.empty:
                                    data_by_load[load_level][scenario_name]['otjae'] = [otjae_power]
                            except Exception as e:
                                print(f"Error loading docker_otjae procfs/jmeter: {e}")

        data_by_env[env_name if env_name else 'flat'] = dict(data_by_load)

    return data_by_env


def plot_all_load_levels_in_one_row(data_by_env, output_path, scenario_suffixes=None, custom_labels=None, show_rittal=True):
    """
    Plots a single row of boxplots, one for each load level, each showing Rittal and Powercap data for selected scenarios.
    Groups all data for each scenario (across all runs) into a single boxplot per scenario per load level.
    
    Parameters are environment-aware - expects data_by_env[env][load_level][scenario] structure.
    
    scenario_suffixes: list of scenario suffixes to include and order.
    custom_labels: dict mapping scenario suffix to label.
    show_rittal: bool, whether to show Rittal (Pem) values or not.
    """
    n_levels = 0
    for env_data in data_by_env.values():
        numeric_items = [(k, v) for k, v in env_data.items() if k.isdigit()]
        n_levels = max(n_levels, len(numeric_items))
    if n_levels == 0:
        print("No load level data to plot.")
        return
    
    fig_width = min(18, 3.5 * n_levels)
    fig_height = 9
    fig, axes = plt.subplots(1, n_levels, figsize=(fig_width, fig_height), sharey=True)
    if n_levels == 1:
        axes = [axes]

    # Collect all sorted load levels across all environments
    all_load_levels = set()
    for env_data in data_by_env.values():
        numeric_items = [(k, v) for k, v in env_data.items() if k.isdigit()]
        all_load_levels.update(k for k, v in numeric_items)
    sorted_loads = sorted(all_load_levels, key=lambda x: int(x))

    for ax, load_level in zip(axes, sorted_loads):
        box_data = []
        box_labels = []
        for env_name, env_data in data_by_env.items():
            scenario_dict = env_data.get(load_level, {})
            if not scenario_dict:
                continue
            for suffix in (scenario_suffixes if scenario_suffixes is not None else scenario_dict.keys()):
                matching = [k for k in scenario_dict.keys() if k.endswith(suffix)] if scenario_suffixes else [suffix]
                all_rittal = []
                all_powercap = []
                all_kepler = []
                all_scaphandre = []
                all_powerapi = []
                all_joularjx = []
                all_otjae = []
                for scenario in matching:
                    sdata = scenario_dict.get(scenario, {})
                    all_rittal.extend(series for _run_label, series in sdata.get('rittal', []))
                    all_powercap.extend(series for _run_label, series in sdata.get('powercap', []))
                    all_kepler.extend(series for _run_label, series in sdata.get('kepler', []))
                    all_scaphandre.extend(series for _run_label, series in sdata.get('scaphandre', []))
                    all_powerapi.extend(series for _run_label, series in sdata.get('powerapi', []))
                    all_joularjx.extend(series for _run_label, series in sdata.get('joularjx', []))
                    if 'otjae' in sdata:
                        all_otjae.extend(series for _run_label, series in sdata.get('otjae', []))
                if show_rittal and all_rittal:
                    combined = pd.concat(all_rittal, ignore_index=True)
                    env_prefix = f'{env_name}: ' if env_name and env_name != 'flat' else ''
                    #label = custom_labels.get(suffix, suffix) if custom_labels else suffix
                    box_data.append(combined)
                    box_labels.append(f'$P_{{EM}}$')
                if all_powercap:
                    combined = pd.concat(all_powercap, ignore_index=True)
                    env_prefix = f'{env_name}: ' if env_name and env_name != 'flat' else ''
                    #label = custom_labels.get(suffix, suffix) if custom_labels else suffix
                    box_data.append(combined)
                    box_labels.append(f'$P_{{S}}$')
                if all_kepler:
                    combined = pd.concat(all_kepler, ignore_index=True)
                    env_prefix = f'{env_name}: ' if env_name and env_name != 'flat' else ''
                    #label = custom_labels.get(suffix, suffix) if custom_labels else suffix
                    box_data.append(combined)
                    box_labels.append(f'$P_{{P_{{K}}}}$')
                if all_scaphandre:
                    combined = pd.concat(all_scaphandre, ignore_index=True)
                    env_prefix = f'{env_name}: ' if env_name and env_name != 'flat' else ''
                    #label = custom_labels.get(suffix, suffix) if custom_labels else suffix
                    box_data.append(combined)
                    box_labels.append(f'$P_{{P_{{Sc}}}}$')
                if all_powerapi:
                    combined = pd.concat(all_powerapi, ignore_index=True)
                    env_prefix = f'{env_name}: ' if env_name and env_name != 'flat' else ''
                    #label = custom_labels.get(suffix, suffix) if custom_labels else suffix
                    box_data.append(combined)
                    box_labels.append(f'$P_{{P_{{PA}}}}$')
                if all_joularjx:
                    combined = pd.concat(all_joularjx, ignore_index=True)
                    env_prefix = f'{env_name}: ' if env_name and env_name != 'flat' else ''
                    #label = custom_labels.get(suffix, suffix) if custom_labels else suffix
                    box_data.append(combined)
                    box_labels.append(f'$P_{{P_{{J}}}}$')
                if all_otjae:
                    combined = pd.concat(all_otjae, ignore_index=True)
                    env_prefix = f'{env_name}: ' if env_name and env_name != 'flat' else ''
                    #label = custom_labels.get(suffix, suffix) if custom_labels else suffix
                    box_data.append(combined)
                    box_labels.append(f'$P_{{P_{{O}}}}$')
        if box_data:
            bp = ax.boxplot(box_data, patch_artist=True, medianprops=dict(color="grey", linewidth=2.5),
                            showmeans=True,
                            meanprops={"marker": "x", "markerfacecolor": "red", "markeredgecolor": "red", "markersize": 10})
            ax.set_xticklabels(box_labels, rotation=0, ha='right', fontsize=18)
            for patch in bp['boxes']:
                patch.set_facecolor('lightgrey')
        ax.set_title(f'Load: {int(load_level) * 3} T/s', fontsize=22)
        ax.set_xlabel('', fontsize=18)
        ax.tick_params(axis='y', labelsize=16)
        ax.tick_params(axis='x', labelsize=16)
        ax.yaxis.grid(True, linestyle='--', alpha=0.7)
    axes[0].set_ylabel('Power (Watts)', fontsize=20)
    plt.tight_layout()
    plt.subplots_adjust(bottom=0.18, top=0.90, left=0.07, right=0.98)
    plt.savefig(output_path, format='pdf', bbox_inches='tight')
    plt.close()
    print(f"Saved combined boxplot to {output_path}")


def main():
    """
    Main entry point: collects data, generates and saves the boxplot PDF for all load levels.
    Handles results per environment (Container, VM, etc.) separately.
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

    exp_results = Path(__file__).resolve().parent
    env_names = [e for e in discover_environments(exp_results) if e in ("Container", "VM")]
    if not env_names:
        env_names = [None]

    # Collect environment data for the LaTeX table
    env_data_store = {}

    for env_name in env_names:
        env_key = env_name if env_name else 'flat'

        load_level_map = build_run_dirs(exp_results, environment=env_name) if env_name else build_run_dirs(exp_results)

        # Prepare data structures for this environment
        data_by_load = defaultdict(lambda: defaultdict(lambda: {'rittal': [], 'powercap': [], 'kepler': [], 'scaphandre': [], 'powerapi': [], 'joularjx': [], 'otjae': []}))

        # --- Step 1: Collect pcpumin/pcpumax ---
        pcpumin = None
        pcpumax = None
        for ll in ['0', '560']:
            dirs = load_level_map.get(ll, [])
            powercap_means = []
            for run_path in dirs:
                for scenario_dir in run_path.iterdir():
                    if not scenario_dir.is_dir():
                        continue
                    if not scenario_matches(scenario_dir.name, 'tools'):
                        continue
                    jb = get_jmeter_time_bounds(str(scenario_dir), trim_seconds)
                    pf = list(scenario_dir.glob('**/powercap_*.csv'))
                    for fp in pf:
                        try:
                            pd2 = load_power_data(str(fp), trim_seconds, jb)
                            if 'Power' in pd2.columns:
                                powercap_means.append(pd2['Power'].mean())
                        except Exception as e:
                            print(f"Error loading {fp}: {e}")
            if powercap_means:
                mean_val = float(np.mean(powercap_means))
                if ll == '0':
                    pcpumin = mean_val
                elif ll == '560':
                    pcpumax = mean_val
        data_by_load['pcpumin'] = pcpumin
        data_by_load['pcpumax'] = pcpumax
        label = f"[{env_name}] " if env_name else ""
        print(f"{label}Min Mean CPU Power: {pcpumin} W, Max Mean CPU Power: {pcpumax} W")

        # --- Step 2: Process all scenarios ---
        # Values are tagged with their run label (e.g. "690", "690_run2") so
        # the LaTeX table below can compute repetition-level statistics
        # (mean/std/CI) instead of pooling raw samples across repetitions.
        # This also fixes a pre-existing bug where OTJAE's assignment
        # (`... = [op]`, instead of append) silently discarded all but the
        # last-processed repetition's data for each load level.
        for load_level, dirs in load_level_map.items():
            if included_load_levels is not None and load_level not in included_load_levels:
                continue
            for run_path in dirs:
                run_label = run_path.name
                for scenario_dir in run_path.iterdir():
                    if not scenario_dir.is_dir():
                        continue
                    scenario_name = scenario_dir.name
                    if scenario_suffixes is not None and not scenario_matches_any(scenario_name, scenario_suffixes):
                        continue
                    jb = get_jmeter_time_bounds(str(scenario_dir), trim_seconds)
                    if scenario_matches(scenario_name, 'joularjx'):
                        try:
                            jd = parse_joularjx_power(scenario_dir, trim_seconds=trim_seconds, jmeter_bounds=jb)
                            if not jd.empty:
                                data_by_load[load_level][scenario_name]['joularjx'].append((run_label, jd['Power']))
                        except Exception as e:
                            print(f"Error loading joularjx: {e}")
                    if scenario_matches(scenario_name, 'tools'):
                        rfs = list(scenario_dir.glob('**/rittal_*.csv'))
                        pcf = list(scenario_dir.glob('**/powercap_*.csv'))
                        for fp in rfs:
                            try:
                                pd2 = load_rittal_data(str(fp), trim_seconds, jb)
                                if 'Power (Watts)' in pd2.columns:
                                    data_by_load[load_level][scenario_name]['rittal'].append((run_label, pd2['Power (Watts)']))
                            except Exception as e:
                                print(f"Error loading {fp}: {e}")
                        for fp in pcf:
                            try:
                                pd2 = load_power_data(str(fp), trim_seconds, jb)
                                if 'Power' in pd2.columns:
                                    data_by_load[load_level][scenario_name]['powercap'].append((run_label, pd2['Power']))
                            except Exception as e:
                                print(f"Error loading {fp}: {e}")
                    kepler_files = list(scenario_dir.glob('**/http_logger_spring_docker_kepler*.csv'))
                    if kepler_files:
                        elp = scenario_dir / 'logs' / 'experiment_log.jsonl'
                        spids = extract_service_pids(str(elp)) if elp.exists() else []
                        for fp in kepler_files:
                            try:
                                kd = parse_kepler_http_logger(str(fp), spids, trim_seconds, jb)
                                if not kd.empty:
                                    data_by_load[load_level][scenario_name]['kepler'].append((run_label, kd['Power']))
                            except Exception as e:
                                print(f"Error loading {fp}: {e}")
                    scaphandre_files = list(scenario_dir.glob('**/http_logger_spring_docker_scaphandre*.csv')) + \
                                       list(scenario_dir.glob('**/http_logger_spring_vm_scaphandre*.csv'))
                    if scaphandre_files:
                        elp = scenario_dir / 'logs' / 'experiment_log.jsonl'
                        spids = extract_service_pids(str(elp)) if elp.exists() else []
                        for fp in scaphandre_files:
                            try:
                                sd = parse_scaphandre_http_logger(str(fp), spids, trim_seconds, jb)
                                if not sd.empty:
                                    data_by_load[load_level][scenario_name]['scaphandre'].append((run_label, sd['Power']))
                            except Exception as e:
                                print(f"Error loading {fp}: {e}")
                    if scenario_matches(scenario_name, 'powerapi'):
                        pafs = list(scenario_dir.glob('**/smartwattsresults/localhost-/energy.slice/**/docker-*.scope/PowerReport.csv')) + \
                               list(scenario_dir.glob('**/smartwattsresults/localhost-/energy.slice/**/vm-*.scope/PowerReport.csv'))
                        pafs = filter_application_powerapi_files(pafs)
                        for fp in pafs:
                            try:
                                pad = parse_powerapi_report(str(fp), trim_seconds, jb)
                                if not pad.empty:
                                    data_by_load[load_level][scenario_name]['powerapi'].append((run_label, pad['Power']))
                            except Exception as e:
                                print(f"Error loading {fp}: {e}")
                    if scenario_matches(scenario_name, 'otjae'):
                        pm1 = data_by_load.get('pcpumin', None)
                        pm2 = data_by_load.get('pcpumax', None)
                        if pm1 is not None and pm2 is not None:
                            try:
                                op = process_docker_otjae(scenario_dir, trim_seconds, pm1, pm2)
                                if op is not None and not op.empty:
                                    data_by_load[load_level][scenario_name]['otjae'].append((run_label, op))
                            except Exception as e:
                                print(f"Error loading docker_otjae: {e}")

        # --- Plot per environment ---
        numeric_items = [(k, v) for k, v in data_by_load.items() if k.isdigit()]
        sorted_loads = sorted(numeric_items, key=lambda x: int(x[0]))
        env_output = f"./process_power_consumption_boxplots_{env_key}_all_loads.pdf"
        plot_all_load_levels_in_one_row({env_key: dict(data_by_load)}, env_output, scenario_suffixes=scenario_suffixes, custom_labels=custom_labels, show_rittal=show_rittal)
        print(f"Environment {env_name}: Saved plot to {env_output}")

        # Store for LaTeX table generation
        env_data_store[env_key] = data_by_load
        print(f"Environment {env_name}: Processed {len(numeric_items)} load levels")

    # Print one LaTeX table per environment
    print("\n=== LaTeX Tables (Per Environment) ===")

    def pct_stats(tool_by_run, ps_by_run):
        """Per-run ratio (tool/P_S) -> summarize_repetitions, so the printed
        percentage carries the same repetition-level std as the W values."""
        common_runs = sorted(set(tool_by_run) & set(ps_by_run))
        ratios = [tool_by_run[r] / ps_by_run[r] * 100 for r in common_runs if ps_by_run[r] != 0]
        return summarize_repetitions(ratios)

    def fmt_pct(stats):
        if stats["mean"] is None:
            return "-"
        return fmt_mean_std(stats["mean"], stats["std"], unit="")

    tool_defs = [("kepler", "Kepler"), ("scaphandre", "Scaphandre"), ("powerapi", "PowerAPI"), ("otjae", "OTJAE"), ("joularjx", "JoularJX")]
    tools = tuple(t for t, _ in tool_defs)
    # Per-run relative error of each tool vs. P_S, i.e. (P_tool - P_S)/P_S,
    # computed against that load level's own P_S mean and pooled across all
    # load levels. Comparing raw pooled Watts as two independent samples
    # conflates load-driven power scaling with attribution error, since P_S
    # itself ranges over ~50W across load levels; centering each tool
    # repetition against its own load level's P_S mean first (mirroring the
    # CPU-utilization-overhead script's deviation-from-baseline design)
    # isolates the accuracy signal before pooling, so a paired dz +
    # Wilcoxon signed-rank (one-sample-style, vs 0) is used instead of an
    # independent-samples test.
    pooled_relative_error_by_tool = {env_key: {t: [] for t in tools} for env_key in env_data_store}

    for env_key, data_by_load in env_data_store.items():
        numeric_items = [(k, v) for k, v in data_by_load.items() if k.isdigit()]
        sorted_loads = sorted(numeric_items, key=lambda x: int(x[0]))
        n_rows = len(sorted_loads)

        # Precompute every row first so tool columns with no data at all
        # (across every load level) can be dropped from the table entirely.
        rows = []
        for load_level, scenario_dict in sorted_loads:
            ps_by_run = per_run_means(scenario_dict, 'tools', 'powercap')
            tool_by_run = {t: per_run_means(scenario_dict, t, t) for t in tools}

            ps_stats = summarize_repetitions(list(ps_by_run.values()))
            ps_mean_this_level = ps_stats["mean"]
            for t in tools:
                if ps_mean_this_level:
                    pooled_relative_error_by_tool[env_key][t].extend(
                        (v - ps_mean_this_level) / ps_mean_this_level for v in tool_by_run[t].values()
                    )
            tool_stats = {t: summarize_repetitions(list(tool_by_run[t].values())) for t in tools}
            rows.append({
                "load_label": f"{int(load_level)*3}",
                "ps_cell": fmt_mean_std(ps_stats["mean"], ps_stats["std"], unit=""),
                "tool_cells": {t: fmt_mean_std(tool_stats[t]["mean"], tool_stats[t]["std"], unit="") for t in tools},
                "tool_pct_cells": {t: fmt_pct(pct_stats(tool_by_run[t], ps_by_run)) for t in tools},
            })

        active_tools = [(t, label) for t, label in tool_defs if any(r["tool_cells"][t] != "-" for r in rows)]
        n_cols = 2 + 2 * len(active_tools)

        print(f"% {env_key} Environment")
        print("\\begin{table*}[!htbp]")
        print("    \\centering")
        print(f"    \\caption{{Mean process power consumption by load level (Environment: {env_key}, Runtime setup: RS1); cells show mean$\\pm$std across n=3 repetitions}}")
        print(f"    \\label{{tab:process_power_depending_throughput_{env_key.lower()}}}")
        print("    \\resizebox{\\textwidth}{!}{%")
        print("        \\begin{tabular}{ |" + "c|" * n_cols + " }")
        print("            \\hline")
        tool_headers = " & ".join(f"\\multicolumn{{2}}{{|c|}}{{{label}}}" for _, label in active_tools)
        print(f"            \\multirow{{2}}{{*}}{{Load (T/s)}} & \\multirow{{2}}{{*}}{{P\\textsubscript{{S}} (W)}} & {tool_headers} \\\\")
        print("            \\hhline{|~|~|" + "-|" * (2 * len(active_tools)) + "}")
        subheaders = " & ".join("P\\textsubscript{P} (W) &  $\\Delta$ (\\%)" for _ in active_tools)
        print(f"            &  & {subheaders} \\\\")
        print("            \\hline")

        for row_idx, row in enumerate(rows):
            tool_cells = " & ".join(f"{row['tool_cells'][t]} & {row['tool_pct_cells'][t]}" for t, _ in active_tools)
            print(f"            {row['load_label']} & {row['ps_cell']} & {tool_cells} \\\\")
            if row_idx < n_rows - 1:
                print("            \\hhline{|" + "-|" * n_cols + "}")
            else:
                print("            \\hline")
        print("        \\end{tabular}")
        print("    }")
        print("\\end{table*}")

    # --- Statistical notes (tool vs P_S, load-centered relative error ---
    # --- (P_tool-P_S)/P_S, pooled across all load levels; one-sample ---
    # --- Wilcoxon signed-rank test vs 0 -- see comment above ---
    # --- pooled_relative_error_by_tool for why raw independent-samples ---
    # --- pooling across load levels is unsuitable) ---
    for env_key, tool_values in pooled_relative_error_by_tool.items():
        for tool_name, rel_errors in tool_values.items():
            if not rel_errors:
                continue
            d_paired = cohens_d_paired(rel_errors)
            wilcoxon = wilcoxon_signed_rank_exact(rel_errors)
            print(f"% Statistical note ({env_key}, {tool_name} vs P_S), one-sample Wilcoxon test on relative error (P_tool-P_S)/P_S from the same-load-level P_S mean (n={wilcoxon['n_nonzero']}):")
            if d_paired is not None:
                print(f"%   Cohen's dz = {d_paired:.3f}")
            if wilcoxon["p_two_sided"] is not None:
                print(f"%   Wilcoxon signed-rank: W+={wilcoxon['statistic']:.1f}, p={wilcoxon['p_two_sided']:.4f} (p_floor={wilcoxon['p_floor']:.4f})")


# Run the script if executed directly
if __name__ == "__main__":
    main()
