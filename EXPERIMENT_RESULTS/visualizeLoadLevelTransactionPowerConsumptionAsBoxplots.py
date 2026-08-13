"""
visualizeLoadLevelTransactionPowerConsumptionAsBoxplots.py

Compute per-transaction power consumption estimates for OTJAE and JoularJX and produce boxplots grouped by load level. This script contains specialized parsers for OTJAE log formats and JoularJX per-method power files, and uses procfs + powercap harmonization to attribute system power to transactions.

Key features
- `parse_otjae_transaction_resource` extracts per-second transaction resource usage from OTJAE logs.
- `parse_joularjx_transaction_power` reads JoularJX method-level CSVs and computes per-invocation and per-second power estimates.
"""

# Global constants for OTJAE power calculations
MEMORY_POWER_W_PER_GB = 0.392
NETWORK_POWER_W_PER_GB = 1.0
STORAGE_POWER_W_PER_TB = 1.2

 # --- Boxplot configuration ---
# Set to 'per_second' or 'per_invocation' to control which data is shown in the boxplots
BOXPLOT_DATA_MODE = 'per_invocation'  # Options: 'per_second', 'per_invocation'

import pandas as pd
import numpy as np
import matplotlib.pyplot as plt
from pathlib import Path
from collections import defaultdict
import re
import csv
import threading

from shared import (
    build_run_dirs,
    trim_time_series,
    load_rittal_data,
    calculate_power_from_energy,
    load_power_data,
    get_jmeter_time_bounds,
    extract_service_pids,
    parse_joularjx_grouped_power,
    parse_joularjx_power,
    ensure_joularjx_unzipped,
    discover_environments,
    scenario_matches,
    scenario_matches_any,
    summarize_repetitions,
    cohens_d_paired,
    wilcoxon_signed_rank_exact,
    fmt_mean_std,
)

def calculate_otjae_transaction_power_cpu(scenario_dir, procfs_file, service_pids, otjae_per_second, jmeter_bounds=None, n_cores=80, ticks_per_sec=100, data_by_load=None):
    """
    Calculate per-transaction power for OTJAE using CPU time fraction per second and process power from procfs.
    otjae_per_second: output of parse_otjae_transaction_resource (per-second resource demand)
    Returns: dict {transaction: [P_transaction_per_sec, ...]}
    """
    # The transaction CPU power follows Eq. 17,
    #   P_CPU_T = (CPU_UTIL_T / CPU_UTIL_P) x P_CPU_P,
    # and Eq. 13 defines P_CPU_P = (CPU_UTIL_P / CPU_UTIL) x P_CPU. The
    # process terms therefore cancel, leaving
    #   P_CPU_T = (transaction CPU time / system CPU time) x P_CPU,
    # where P_CPU is the CPU-only system power of Eq. 12. The *full* process
    # power must not be used as the multiplier here: it also carries the
    # process memory and storage power, which would then be counted a second
    # time when the transaction's own memory/network/storage power is added
    # below (Eq. 19).
    pcpumin = data_by_load.get('pcpumin', None)
    pcpumax = data_by_load.get('pcpumax', None)
    if pcpumin is None or pcpumax is None:
        # fallback: try to estimate from process power min/max
        pcpumin = 0
        pcpumax = 1

    # System CPU time and CPU-only system power per second (from /proc/stat)
    proc_util, sys_df, _, _ = parse_procfs_data(procfs_file, service_pids, n_cores=n_cores, ticks_per_sec=ticks_per_sec, jmeter_bounds=jmeter_bounds)
    if sys_df is None or sys_df.empty:
        return {}
    sys_df = sys_df.copy()
    sys_df['sec'] = sys_df['datetime'].apply(lambda ts: int(ts.timestamp()))
    per_sec = sys_df.groupby('sec').agg(
        delta_cpu=('delta_cpu', 'sum'),
        interval=('interval', 'sum'),
    )
    sys_util_norm = (per_sec['delta_cpu'] / per_sec['interval'] / n_cores).clip(lower=0, upper=1)
    cpu_time_per_sec = per_sec['delta_cpu']
    cpu_power_per_sec = pcpumin + sys_util_norm * (pcpumax - pcpumin)

    # otjae_per_second, cpu_time_per_sec, and cpu_power_per_sec are all keyed
    # by the same real Unix-epoch second, so they can be joined directly
    # without any relative shifting.
    tx_power_per_invocation = {}
    tx_power_per_second = {}
    for sec, txs in otjae_per_second.items():
        sys_cpu = cpu_time_per_sec.get(sec, None)
        P_cpu_sys = cpu_power_per_sec.get(sec, None)
        if sys_cpu is None or sys_cpu == 0 or P_cpu_sys is None:
            continue
        for tx, vals in txs.items():
            cpu_tx = vals['cpu']
            mem_tx = vals.get('mem', 0)
            net_tx = vals.get('net', 0)
            disk_tx = vals.get('disk', 0)
            num_invocations = vals.get('count', 0)
            # Convert cpu_tx from nanoseconds to seconds
            cpu_tx_sec = cpu_tx / 1e9
            # Convert mem_tx (bytes) to GB
            mem_tx_gb = mem_tx / (1024 ** 3)
            # Convert net_tx (bytes) to GB
            net_tx_gb = net_tx / (1024 ** 3)
            # Convert disk_tx (bytes) to TB
            disk_tx_tb = disk_tx / (1024 ** 4)
            # Calculate total transaction power for this second (all invocations)
            if sys_cpu > 0:
                P_tx_cpu_total = (cpu_tx_sec / sys_cpu) * P_cpu_sys
            else:
                P_tx_cpu_total = 0
            P_tx_mem_total = mem_tx_gb * MEMORY_POWER_W_PER_GB
            P_tx_net_total = net_tx_gb * NETWORK_POWER_W_PER_GB
            P_tx_disk_total = disk_tx_tb * STORAGE_POWER_W_PER_TB
            P_tx_total = P_tx_cpu_total + P_tx_mem_total + P_tx_net_total + P_tx_disk_total
            # Calculate per-invocation value (for boxplots)
            if num_invocations > 0:
                P_tx = P_tx_total / num_invocations
            else:
                P_tx = 0
            if tx not in tx_power_per_invocation:
                tx_power_per_invocation[tx] = []
            if tx not in tx_power_per_second:
                tx_power_per_second[tx] = []
            tx_power_per_invocation[tx].append(P_tx)
            tx_power_per_second[tx].append(P_tx_total)
    return {'per_invocation': tx_power_per_invocation, 'per_second': tx_power_per_second}

def parse_otjae_transaction_resource(log_file, jmeter_bounds=None):
    """
    Parse OTJAE docker_compose_logs_*.txt file for transaction resource demand, filtering by JMeter steady-state.
    Returns a dict: {transaction: {'cpu': ..., 'mem': ..., 'net': ..., 'disk': ..., 'count': ...}}
    """
    from collections import defaultdict
    # Get bounds in ms since epoch
    if jmeter_bounds is not None and all(jmeter_bounds):
        start, end = jmeter_bounds
        start_ms = int(start.value // 10**6)
        end_ms = int(end.value // 10**6)
    else:
        start_ms = end_ms = None
    per_second = defaultdict(lambda: defaultdict(lambda: {'cpu': 0, 'mem': 0, 'net': 0, 'disk': 0, 'count': 0}))

    # Compile regex patterns outside the loop
    logsystemtime_pattern = re.compile(r'io\.retit\.logsystemtime=([^,}}]+)')
    startthread_pattern = re.compile(r'io\.retit\.startthread=([^,}}]+)')
    endthread_pattern = re.compile(r'io\.retit\.endthread=([^,}}]+)')
    server_span_pattern = re.compile(r'\b[\da-f]{16} SERVER \[tracer:')
    keyval_pattern = re.compile(r'(\w[\w.]+)=([^,}}]+)')

    # Batch read all lines
    with open(log_file, 'r', encoding='utf-8', errors='ignore') as f:
        lines = f.readlines()

    # Batch filter relevant lines
    filtered_lines = [
        line for line in lines
        if ('io.retit.logsystemtime' in line and 'AttributesMap' in line and server_span_pattern.search(line))
    ]

    # Process only filtered lines
    for line in filtered_lines:
        # Extract logsystemtime
        m_logsystem = logsystemtime_pattern.search(line)
        if not m_logsystem:
            continue
        try:
            logsystemtime = int(m_logsystem.group(1))
        except Exception:
            continue
        if start_ms is not None and end_ms is not None:
            if not (start_ms <= logsystemtime <= end_ms):
                continue

        # Extract startthread and endthread
        m_startthread = startthread_pattern.search(line)
        m_endthread = endthread_pattern.search(line)
        if not m_startthread or not m_endthread:
            continue
        try:
            startthread = int(m_startthread.group(1))
            endthread = int(m_endthread.group(1))
        except Exception:
            continue
        if startthread + endthread == 0:
            continue
        if startthread != endthread:
            continue

        # Extract all key-value pairs (attributes)
        attributes = dict(keyval_pattern.findall(line))
        method = attributes.get('http.request.method')
        if not method:
            continue
        key = f"{method}"
        # Calculate deltas — require both endpoints; return 0 if either is missing
        def calc_delta(attr, startk, endk):
            start_val = attributes.get(startk)
            end_val = attributes.get(endk)
            if start_val is None or end_val is None:
                return 0
            try:
                return max(int(end_val) - int(start_val), 0)
            except Exception:
                return 0
        cpu = calc_delta(attributes, 'io.retit.startcputime', 'io.retit.endcputime')
        mem = calc_delta(attributes, 'io.retit.startheapbyteallocation', 'io.retit.endheapbyteallocation')
        disk = calc_delta(attributes, 'io.retit.startdiskreaddemand', 'io.retit.enddiskreaddemand') + \
               calc_delta(attributes, 'io.retit.startdiskwritedemand', 'io.retit.enddiskwritedemand')
        net = calc_delta(attributes, 'io.retit.startnetworkreaddemand', 'io.retit.endnetworkreaddemand') + \
              calc_delta(attributes, 'io.retit.startnetworkwritedemand', 'io.retit.endnetworkwritedemand')
        # Use the second (rounded from ms) as the time bin
        sec = logsystemtime // 1000
        per_second[sec][key]['cpu'] += cpu
        per_second[sec][key]['mem'] += mem
        per_second[sec][key]['disk'] += disk
        per_second[sec][key]['net'] += net
        per_second[sec][key]['count'] += 1
    return per_second

_JOULARJX_TRANSACTION_GROUPS = ["GET", "POST", "DELETE"]


def _joularjx_transaction_group(method_name: str) -> str | None:
    """Map a JoularJX method name to its transaction-type group, or ``None``
    if it isn't one of the tracked REST methods."""
    if method_name.endswith(".getData"):
        return "GET"
    if method_name.endswith(".postData"):
        return "POST"
    if method_name.endswith(".deleteData"):
        return "DELETE"
    return None


def parse_joularjx_transaction_power(methods_dir, jmeter_bounds=None):
    """
    Parse all JoularJX filtered-methods-power.csv files in *methods_dir* into
    per-transaction-type (GET/POST/DELETE) power series, trimmed to
    *jmeter_bounds* if given.

    Delegates file discovery, the fixed-1s-power-assumption correction, and
    trimming to shared.parse_joularjx_grouped_power, so the same policy is
    used here as by the process-level JoularJX parser in shared.py.

    Returns a dict: {'per_invocation': {transaction_type: [...]}, 'per_second': {transaction_type: [...]}, 'total_energy': {transaction_type: total_energy}, 'steady_state_time': steady_state_time, 'total_energy_from_file': {transaction_type: total_energy}}
    """
    if not Path(methods_dir).is_dir():
        return {}

    # --- Validation: read total energy file(s), if available ---
    total_methods_dir = Path(methods_dir).parent.parent / "total" / "methods"
    total_energy_from_file = defaultdict(float)
    for total_energy_file in sorted(total_methods_dir.glob("joularJX-*-filtered-methods-energy.csv")):
        try:
            with open(total_energy_file, "r", encoding="utf-8") as f:
                reader = csv.reader(f)
                for row in reader:
                    if len(row) == 2:
                        method, energy = row
                        group = _joularjx_transaction_group(method)
                        if group is not None:
                            total_energy_from_file[group] += float(energy)
        except Exception as e:
            print(f"[JoularJX Validation] Error reading total energy file {total_energy_file}: {e}")
    if not total_energy_from_file:
        print("[JoularJX Validation] No total energy file found for validation.")

    grouped = parse_joularjx_grouped_power(
        methods_dir,
        "joularJX-*-filtered-methods-power.csv",
        _joularjx_transaction_group,
        _JOULARJX_TRANSACTION_GROUPS,
        jmeter_bounds=jmeter_bounds,
    )

    per_invocation = defaultdict(list)
    per_second = defaultdict(list)
    total_energy = defaultdict(float)
    steady_state_time = None
    for ttype in _JOULARJX_TRANSACTION_GROUPS:
        df = grouped.get(ttype)
        if df is None or df.empty:
            continue
        per_second[ttype] = df["Power"].tolist()
        per_invocation[ttype] = [
            (power / count) if count > 0 else 0.0
            for power, count in zip(df["Power"], df["Count"])
        ]
        total_energy[ttype] = float(df["Energy"].sum())
        if len(df) > 1:
            span = (df["datetime"].max() - df["datetime"].min()).total_seconds()
            if steady_state_time is None or span > steady_state_time:
                steady_state_time = span

    # --- Validation: compare calculated total_energy with file values and print results ---
    if total_energy_from_file:
        print("[JoularJX Validation] Comparing calculated total_energy with total file values:")
        for ttype in _JOULARJX_TRANSACTION_GROUPS:
            calc_val = total_energy.get(ttype, None)
            file_val = total_energy_from_file.get(ttype, None)
            if calc_val is not None and file_val is not None:
                diff = calc_val - file_val
                rel_diff = (diff / file_val) * 100 if file_val != 0 else float('inf')
                print(f"  {ttype}: calculated = {calc_val:.2f}, file = {file_val:.2f}, diff = {diff:.2f} ({rel_diff:+.2f}%)")
            elif calc_val is not None:
                print(f"  {ttype}: calculated = {calc_val:.2f}, file = MISSING")
            elif file_val is not None:
                print(f"  {ttype}: calculated = MISSING, file = {file_val:.2f}")

    return {
        'per_invocation': per_invocation,
        'per_second': per_second,
        'total_energy': total_energy,
        'steady_state_time': steady_state_time,
        'total_energy_from_file': dict(total_energy_from_file),
    }

def parse_procfs_joularjx(procfs_file, powercap_files, service_pids, trim_seconds=0, jmeter_bounds=None, n_cores=80, ticks_per_sec=100):
    #print(f"\n[DEBUG] --- parse_procfs_joularjx ---")
    #print(f"procfs_file: {procfs_file}")
    #print(f"powercap_files: {powercap_files}")
    #print(f"service_pids: {service_pids}")
    """
    Parses procfs CSV and powercap CSVs, computes process-specific power consumption over time.
    Returns a DataFrame with columns: ['datetime', 'Power'] for the process.
    """
    import pandas as pd
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

    # Attribute the system-level CPU power to the monitored process by its
    # share of the CPU time actually consumed on the system (CPU_UTIL_P /
    # CPU_UTIL), as prescribed by the OTJAE process model. With a single
    # container that has uncontended access to the server (RS1) this share is
    # close to one, but applying it explicitly keeps this calculation
    # consistent with the RS2/RS3 variant in fig_rs2_rs3.py, where two
    # co-located containers must each receive only their own share of the
    # host CPU power.
    if proc_util is None or proc_util.empty:
        return None
    share = proc_util.rename(columns={'util_ratio': 'proc_share'})
    sys_df = pd.merge_asof(
        sys_df.sort_values('datetime'),
        share[['datetime', 'proc_share']].sort_values('datetime'),
        on='datetime',
        direction='nearest',
        tolerance=pd.Timedelta('1s')
    )
    sys_df['proc_share'] = sys_df['proc_share'].fillna(0)
    sys_df['Power'] = sys_df['Power'] * sys_df['proc_share']

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

    # Return as a Series for boxplot, indexed by datetime so callers that
    # need to align it against other per-second series (e.g. the OTJAE
    # transaction-power calculation below) can do so correctly.
    return sys_df[['datetime', 'Power']].dropna().set_index('datetime')['Power']

def collect_data_by_load_level(trim_seconds=0, scenario_suffixes=None, included_load_levels=None):
    """
    Collects and groups all Rittal and Powercap data by environment and load level.
    Aggregates all runs (e.g., 350, 350_run2, 350_run3) for each load level within each environment.
    Only includes scenario subdirectories matching scenario_suffixes if provided.
    If included_load_levels is provided (list of strings), only those load levels are included in the returned data (except for pcpumin/pcpumax, which always use all data).
    Returns a dict: {environment: {load_level: {scenario: {...}}, ...}}
    """
    exp_results = Path(__file__).resolve().parent
    env_names = [e for e in discover_environments(exp_results) if e in ("Container", "VM")]
    if not env_names:
        env_names = [None]

    data_by_env = {}

    for env_name in env_names:
        env_key = env_name if env_name else 'flat'
        load_level_map = build_run_dirs(exp_results, environment=env_name) if env_name else build_run_dirs(exp_results)
        data_by_load = defaultdict(lambda: defaultdict(lambda: {'rittal': [], 'powercap': [], 'joularjx_tx': {}, 'otjae_tx': {}}))

        label = f"[{env_name}] " if env_name else ""

        # --- Step 1: Find all 'tools' scenarios and their load levels for baseline power ---
        tool_load_levels = {}
        for ll_str, dirs_list in load_level_map.items():
            if not str(ll_str).isdigit():
                continue
            has_tools = False
            for run_path in dirs_list:
                if has_tools:
                    break
                for scenario_dir in run_path.iterdir():
                    if scenario_dir.is_dir() and scenario_matches(scenario_dir.name, 'tools'):
                        has_tools = True
                        break
            if has_tools:
                tool_load_levels[int(ll_str)] = []

        pcpumin = None
        pcpumax = None
        if tool_load_levels:
            for target_ll in [min(tool_load_levels.keys()), max(tool_load_levels.keys())]:
                dirs = load_level_map.get(str(target_ll), [])
                powercap_means = []
                for run_path in dirs:
                    for scenario_dir in run_path.iterdir():
                        if not scenario_dir.is_dir():
                            continue
                        if not scenario_matches(scenario_dir.name, 'tools'):
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
                    if target_ll == min(tool_load_levels.keys()):
                        pcpumin = mean_val
                    elif target_ll == max(tool_load_levels.keys()):
                        pcpumax = mean_val

        data_by_load['pcpumin'] = pcpumin
        data_by_load['pcpumax'] = pcpumax
        print(f"{label}Min Mean CPU Power: {pcpumin} W, Max Mean CPU Power: {pcpumax} W")

        # --- Step 2: Process all scenarios ---
        import threading
        from threading import Lock
        data_lock = Lock()

        def process_scenario(load_level, scenario_dir):
            scenario_name = scenario_dir.name
            if scenario_suffixes is not None and not scenario_matches_any(scenario_name, scenario_suffixes):
                return
            jmeter_bounds = get_jmeter_time_bounds(str(scenario_dir), trim_seconds)
            if scenario_matches(scenario_name, 'joularjx'):
                ensure_joularjx_unzipped(scenario_dir)
                methods_dirs = list(scenario_dir.glob('**/app/runtime/methods'))
                if methods_dirs:
                    tx_power = parse_joularjx_transaction_power(methods_dirs[0], jmeter_bounds)
                    if tx_power:
                        with data_lock:
                            data_by_load[load_level][scenario_name]['joularjx_tx'] = tx_power
            if scenario_matches(scenario_name, 'tools'):
                rittal_files = list(scenario_dir.glob('**/rittal_*.csv'))
                powercap_files = list(scenario_dir.glob('**/powercap_*.csv'))
                for file_path in rittal_files:
                    try:
                        power_data = load_rittal_data(str(file_path), trim_seconds, jmeter_bounds)
                        if 'Power (Watts)' in power_data.columns:
                            with data_lock:
                                data_by_load[load_level][scenario_name]['rittal'].append(power_data['Power (Watts)'])
                    except Exception as e:
                        print(f"Error loading {file_path}: {e}")
                for file_path in powercap_files:
                    try:
                        power_data = load_power_data(str(file_path), trim_seconds, jmeter_bounds)
                        if 'Power' in power_data.columns:
                            with data_lock:
                                data_by_load[load_level][scenario_name]['powercap'].append(power_data['Power'])
                    except Exception as e:
                        print(f"Error loading {file_path}: {e}")
            if scenario_matches(scenario_name, 'otjae'):
                pcpumin_ev = data_by_load.get('pcpumin', None)
                pcpumax_ev = data_by_load.get('pcpumax', None)
                if pcpumin_ev is not None and pcpumax_ev is not None:
                    log_files = list(scenario_dir.glob('**/docker_compose_logs_*.txt'))
                    tool_suffix = scenario_name.split('_')[-1]
                    procfs_files = list(scenario_dir.glob(f'**/procfs_*_{tool_suffix}*.csv'))
                    experiment_log_path = scenario_dir / 'logs' / 'experiment_log.jsonl'
                    service_pids = extract_service_pids(str(experiment_log_path)) if experiment_log_path.exists() else []
                    if log_files and procfs_files and service_pids:
                        otjae_per_second = parse_otjae_transaction_resource(str(log_files[0]), jmeter_bounds)
                        tx_power = calculate_otjae_transaction_power_cpu(scenario_dir, str(procfs_files[0]), service_pids, otjae_per_second, jmeter_bounds, data_by_load={'pcpumin': pcpumin_ev, 'pcpumax': pcpumax_ev})
                        if tx_power:
                            with data_lock:
                                data_by_load[load_level][scenario_name]['otjae_tx'] = tx_power

        for load_level, dirs in load_level_map.items():
            if included_load_levels is not None and load_level not in included_load_levels:
                continue
            for run_path in dirs:
                for scenario_dir in run_path.iterdir():
                    if not scenario_dir.is_dir():
                        continue
                    t = threading.Thread(target=process_scenario, args=(load_level, scenario_dir))
                    t.start()

        # Wait for all threads to finish
        for t in threading.enumerate():
            if t is not threading.current_thread():
                t.join()

        data_by_env[env_key] = dict(data_by_load)

    return data_by_env


def plot_all_load_levels_in_one_row(data_by_env, output_path, scenario_suffixes=None, custom_labels=None):
    """
    Plots transaction power boxplots per environment and load level.

    data_by_env: {environment: {load_level: {scenario: {...}}}}
    """
    n_levels = 0
    for env_data in data_by_env.values():
        numeric_items = [(k, v) for k, v in env_data.items() if k.isdigit()]
        n_levels = max(n_levels, len(numeric_items))
    if n_levels == 0:
        print("No load level data to plot.")
        return

    boxplots_per_level = 6
    fig_width = max(12, boxplots_per_level * 0.7 * n_levels + 2)
    fig, axes = plt.subplots(1, n_levels, figsize=(fig_width, 6), sharey=True)
    if n_levels == 1:
        axes = [axes]

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
            tx_agg_jx = {'GET': [], 'POST': [], 'DELETE': []}
            tx_agg_ot = {'GET': [], 'POST': [], 'DELETE': []}
            for scenario in scenario_dict:
                sdata = scenario_dict[scenario]
                # joularjx_tx/otjae_tx are keyed by run_label -> tx_power dict;
                # boxplots pool raw values across all repetitions (unlike the
                # LaTeX table below, which reports per-repetition mean+std).
                for run_label, joularjx_tx in sdata.get('joularjx_tx', {}).items():
                    if isinstance(joularjx_tx, dict) and BOXPLOT_DATA_MODE in joularjx_tx:
                        tx_dict = joularjx_tx[BOXPLOT_DATA_MODE]
                    elif isinstance(joularjx_tx, dict) and 'per_invocation' in joularjx_tx:
                        tx_dict = joularjx_tx['per_invocation']
                    else:
                        tx_dict = joularjx_tx
                    for ttype in ['GET', 'POST', 'DELETE']:
                        if ttype in tx_dict and tx_dict[ttype]:
                            tx_agg_jx[ttype].extend(tx_dict[ttype])
                for run_label, otjae_tx in sdata.get('otjae_tx', {}).items():
                    if isinstance(otjae_tx, dict) and BOXPLOT_DATA_MODE in otjae_tx:
                        tx_dict = otjae_tx[BOXPLOT_DATA_MODE]
                    elif isinstance(otjae_tx, dict) and 'per_invocation' in otjae_tx:
                        tx_dict = otjae_tx['per_invocation']
                    else:
                        tx_dict = otjae_tx
                    for ttype in ['GET', 'POST', 'DELETE']:
                        if ttype in tx_dict and tx_dict[ttype]:
                            tx_agg_ot[ttype].extend(tx_dict[ttype])
            for ttype in ['GET', 'POST', 'DELETE']:
                if tx_agg_jx[ttype]:
                    box_data.append(pd.Series(tx_agg_jx[ttype]))
                    box_labels.append(f'${ttype}_{{J}}$')
                if tx_agg_ot[ttype]:
                    box_data.append(pd.Series(tx_agg_ot[ttype]))
                    box_labels.append(f'${ttype}_{{O}}$')
        if box_data:
            bp = ax.boxplot(box_data, patch_artist=True, medianprops=dict(color="grey", linewidth=1.5),
                            showmeans=True,
                            meanprops={"marker": "x", "markerfacecolor": "red", "markeredgecolor": "red", "markersize": 5})
            ax.set_xticklabels(box_labels, rotation=0, ha='right')
            for patch in bp['boxes']:
                patch.set_facecolor('lightgrey')
        ax.set_title(f'Load: {int(load_level) * 3} T/s')
        ax.set_xlabel('')
        ax.yaxis.grid(True, linestyle='--', alpha=0.7)
    axes[0].set_ylabel('Power (Watts)')
    plt.tight_layout()
    plt.savefig(output_path, format='pdf', bbox_inches='tight')
    plt.close()
    print(f"Saved combined boxplot to {output_path}")


def main():
    """
    Main function to collect data and generate plots.
    Handles results per environment (Container, VM, etc.) separately.
    """
    trim_seconds = 60
    scenario_suffixes = None
    custom_labels = {
        "idle_no_tools": "Idle",
        "docker_tools": "",
        "docker_otjae": "",
        "docker_joularjx": "",
    }
    included_load_levels = ["230", "350", "480", "560", "580"]

    exp_results = Path(__file__).resolve().parent
    env_names = [e for e in discover_environments(exp_results) if e in ("Container", "VM")]
    if not env_names:
        env_names = [None]

    env_data_store = {}

    for env_name in env_names:
        env_key = env_name if env_name else 'flat'
        load_level_map = build_run_dirs(exp_results, environment=env_name) if env_name else build_run_dirs(exp_results)
        data_by_load = defaultdict(lambda: defaultdict(lambda: {'rittal': [], 'powercap': [], 'joularjx_tx': {}, 'otjae_tx': {}, 'joularjx_pp': {}, 'otjae_pp': {}}))

        # --- Step 1: Find all 'tools' scenarios and their load levels for baseline power ---
        tool_load_levels = {}
        for ll_str, dirs_list in load_level_map.items():
            if not str(ll_str).isdigit():
                continue
            has_tools = False
            for run_path in dirs_list:
                if has_tools:
                    break
                for scenario_dir in run_path.iterdir():
                    if scenario_dir.is_dir() and scenario_matches(scenario_dir.name, 'tools'):
                        has_tools = True
                        break
            if has_tools:
                tool_load_levels[int(ll_str)] = []

        pcpumin = None
        pcpumax = None
        if tool_load_levels:
            for target_ll in [min(tool_load_levels.keys()), max(tool_load_levels.keys())]:
                dirs = load_level_map.get(str(target_ll), [])
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
                                pass
                if powercap_means:
                    mean_val = float(np.mean(powercap_means))
                    if target_ll == min(tool_load_levels.keys()):
                        pcpumin = mean_val
                    elif target_ll == max(tool_load_levels.keys()):
                        pcpumax = mean_val

        data_by_load['pcpumin'] = pcpumin
        data_by_load['pcpumax'] = pcpumax
        label = f"[{env_name}] " if env_name else ""
        print(f"{label}Min Mean CPU Power: {pcpumin} W, Max Mean CPU Power: {pcpumax} W")

        # --- Step 2: Process scenarios ---
        # Every collected value is tagged with its run label (e.g. "690",
        # "690_run2"), threaded explicitly through process_scenario rather than
        # derived after the fact, so the run-label tagging happens inside the
        # same lock that writes the value. Previously, joularjx_tx/otjae_tx/
        # joularjx_pp/otjae_pp were plain assignments (`= value`), so with 3
        # repetitions racing across threads, only whichever thread finished
        # last for a given load level survived -- silently discarding the
        # other repetitions' data. Keying by run_label fixes this.
        from threading import Lock
        data_lock = Lock()

        def process_scenario(load_level, run_label, scenario_dir):
            scenario_name = scenario_dir.name
            if scenario_suffixes is not None and not any(scenario_name.endswith(suf) for suf in scenario_suffixes):
                return
            jb = get_jmeter_time_bounds(str(scenario_dir), trim_seconds)
            if scenario_matches(scenario_name, 'joularjx'):
                ensure_joularjx_unzipped(scenario_dir)
                methods_dirs = list(scenario_dir.glob('**/app/runtime/methods'))
                if methods_dirs:
                    tx_power = parse_joularjx_transaction_power(methods_dirs[0], jb)
                    if tx_power:
                        with data_lock:
                            data_by_load[load_level][scenario_name]['joularjx_tx'][run_label] = tx_power
                # Process-level P_P (sum of all methods), same computation as the
                # process-level table, so the Delta column compares like with like.
                pp = parse_joularjx_power(scenario_dir, trim_seconds=trim_seconds, jmeter_bounds=jb)
                if not pp.empty:
                    with data_lock:
                        data_by_load[load_level][scenario_name]['joularjx_pp'][run_label] = pp['Power']
            if scenario_matches(scenario_name, 'tools'):
                rfs = list(scenario_dir.glob('**/rittal_*.csv'))
                pcf = list(scenario_dir.glob('**/powercap_*.csv'))
                for fp in rfs:
                    try:
                        pd2 = load_rittal_data(str(fp), trim_seconds, jb)
                        if 'Power (Watts)' in pd2.columns:
                            with data_lock:
                                data_by_load[load_level][scenario_name]['rittal'].append((run_label, pd2['Power (Watts)']))
                    except Exception as e:
                        pass
                for fp in pcf:
                    try:
                        pd2 = load_power_data(str(fp), trim_seconds, jb)
                        if 'Power' in pd2.columns:
                            with data_lock:
                                data_by_load[load_level][scenario_name]['powercap'].append((run_label, pd2['Power']))
                    except Exception as e:
                        pass
            if scenario_matches(scenario_name, 'otjae'):
                pm1 = data_by_load.get('pcpumin', None)
                pm2 = data_by_load.get('pcpumax', None)
                if pm1 is not None and pm2 is not None:
                    lfs = list(scenario_dir.glob('**/docker_compose_logs_*.txt'))
                    tool_suffix = scenario_name.split('_')[-1]
                    pfs = list(scenario_dir.glob(f'**/procfs_*_{tool_suffix}*.csv'))
                    elp = scenario_dir / 'logs' / 'experiment_log.jsonl'
                    spids = extract_service_pids(str(elp)) if elp.exists() else []
                    if lfs and pfs and spids:
                        opds = parse_otjae_transaction_resource(str(lfs[0]), jb)
                        tx_power = calculate_otjae_transaction_power_cpu(scenario_dir, str(pfs[0]), spids, opds, jb, data_by_load={'pcpumin': pm1, 'pcpumax': pm2})
                        if tx_power:
                            with data_lock:
                                data_by_load[load_level][scenario_name]['otjae_tx'][run_label] = tx_power
                    # Process-level P_P, same computation as the process-level table
                    # (procfs CPU share scaled between pcpumin/pcpumax, plus memory
                    # and storage), so the Delta column compares like with like.
                    pp = process_docker_otjae(scenario_dir, trim_seconds, pm1, pm2)
                    if pp is not None and not pp.empty:
                        with data_lock:
                            data_by_load[load_level][scenario_name]['otjae_pp'][run_label] = pp

        threads = []
        for load_level, dirs in load_level_map.items():
            if included_load_levels is not None and load_level not in included_load_levels:
                continue
            for run_path in dirs:
                run_label = run_path.name
                for scenario_dir in run_path.iterdir():
                    if not scenario_dir.is_dir():
                        continue
                    t = threading.Thread(target=process_scenario, args=(load_level, run_label, scenario_dir))
                    threads.append(t)
                    t.start()
        for t in threads:
            t.join()

        env_data_store[env_key] = data_by_load
        print(f"Environment {env_name}: Processed")

        # --- Plot per environment ---
        env_output = f"./transaction_power_consumption_boxplots_{env_key}_all_loads.pdf"
        plot_all_load_levels_in_one_row({env_key: data_by_load}, env_output, scenario_suffixes=scenario_suffixes, custom_labels=custom_labels)
        print(f"Environment {env_name}: Saved plot to {env_output}")

    # --- Print mean values per environment ---
    print("\nMean power values per transaction and scenario (by environment):")
    for env_key, data_by_env in env_data_store.items():
        print(f"\n=== Environment: {env_key} ===")
        for load_level, scenario_dict in data_by_env.items():
            if not str(load_level).isdigit():
                continue
            print(f"\nLoad level: {load_level}")
            for scenario, results in scenario_dict.items():
                for run_label, joularjx_tx in results.get('joularjx_tx', {}).items():
                    if not joularjx_tx:
                        continue
                    ss_time = joularjx_tx.get('steady_state_time', None)
                    teff = joularjx_tx.get('total_energy_from_file', {})
                    if isinstance(joularjx_tx, dict) and 'per_invocation' in joularjx_tx and 'per_second' in joularjx_tx:
                        for ttype in ['GET', 'POST', 'DELETE']:
                            pi = joularjx_tx['per_invocation'].get(ttype, [])
                            ps = joularjx_tx['per_second'].get(ttype, [])
                            te = joularjx_tx.get('total_energy', {}).get(ttype, None)
                            if pi:
                                print(f"  [{run_label}] {scenario} - {ttype}_J: mean per-invocation = {float(np.mean(pi)):.3f} W", end='')
                            if ps:
                                print(f", mean per-second = {float(np.mean(ps)):.3f} W", end='')
                            if te is not None and ss_time and ss_time > 0:
                                print(f", mean total-energy/steady-state = {te/ss_time:.3f} W", end='')
                            if teff.get(ttype) is not None and ss_time and ss_time > 0:
                                print(f", mean file-energy/steady-state = {teff[ttype]/ss_time:.3f} W")
                            elif pi or ps:
                                print()
                    else:
                        for ttype, values in joularjx_tx.items():
                            if values:
                                print(f"  [{run_label}] {scenario} - {ttype}_J: mean = {float(np.mean(values)):.3f} W")
                for run_label, otjae_tx in results.get('otjae_tx', {}).items():
                    if not otjae_tx:
                        continue
                    if isinstance(otjae_tx, dict) and 'per_invocation' in otjae_tx and 'per_second' in otjae_tx:
                        for ttype in ['GET', 'POST', 'DELETE']:
                            pi = otjae_tx['per_invocation'].get(ttype, [])
                            ps = otjae_tx['per_second'].get(ttype, [])
                            if pi:
                                print(f"  [{run_label}] {scenario} - {ttype}_O: mean per-invocation = {float(np.mean(pi)):.3f} W", end='')
                            if ps:
                                print(f", mean per-second = {float(np.mean(ps)):.3f} W")
                            elif pi:
                                print()
                    else:
                        for ttype, values in otjae_tx.items():
                            if values:
                                print(f"  [{run_label}] {scenario} - {ttype}_O: mean = {float(np.mean(values)):.3f} W")

    # --- Combined LaTeX Table (All Environments) ---
    print("\n=== Combined LaTeX Table (All Environments) ===")

    ttypes = ('GET', 'POST', 'DELETE')

    def _session_deltas(ps_by_run_per_ttype, pp_by_run):
        """Per-run (session) Delta = sum(P_T over available transaction types)
        - P_P, for every run present in pp_by_run. Mirrors the original
        semantics (sum over whichever ttypes have data, skip missing), but
        now yields one paired difference per repetition instead of a single
        pooled scalar."""
        deltas = []
        for run_label in sorted(pp_by_run.keys()):
            parts = [ps_by_run_per_ttype[ttype].get(run_label) for ttype in ttypes]
            parts = [p for p in parts if p is not None]
            if not parts:
                continue
            deltas.append(sum(parts) - pp_by_run[run_label])
        return deltas

    def _session_sums(ps_by_run_per_ttype):
        """Per-run (session) sum(P_T over available transaction types), for
        every run label present in any transaction type -- used to report
        mean+-std of Sum(P_T) itself (e.g. for reuse as a point estimate
        with std in other scripts), independent of the Delta-vs-P_P check."""
        run_labels = sorted(set().union(*(d.keys() for d in ps_by_run_per_ttype.values())))
        sums = []
        for run_label in run_labels:
            parts = [ps_by_run_per_ttype[ttype].get(run_label) for ttype in ttypes]
            parts = [p for p in parts if p is not None]
            if not parts:
                continue
            sums.append(sum(parts))
        return sums

    # Collect table_stats for every environment first
    all_env_stats = {}
    # Session-paired Delta (Sum P_T - P_P) across all load levels per
    # environment/tool, for the statistical notes below the table.
    session_deltas_by_tool = {env_key: {'OTJAE': [], 'JoularJX': []} for env_key in env_data_store}

    for env_key, data_by_env in env_data_store.items():
        table_stats = {}
        for load_level, scenario_dict in data_by_env.items():
            if not str(load_level).isdigit():
                continue
            # {ttype: {run_label: per-run mean P_T}}
            otjae_ps_by_run = {t: {} for t in ttypes}
            jx_ps_by_run = {t: {} for t in ttypes}
            otjae_pp_by_run = {}
            jx_pp_by_run = {}
            for scenario, results in scenario_dict.items():
                for run_label, jx in results.get('joularjx_tx', {}).items():
                    if isinstance(jx, dict) and 'per_second' in jx:
                        for ttype, values in jx['per_second'].items():
                            if values and ttype in jx_ps_by_run:
                                jx_ps_by_run[ttype][run_label] = float(np.mean(values))
                for run_label, ot in results.get('otjae_tx', {}).items():
                    if isinstance(ot, dict) and 'per_second' in ot:
                        for ttype, values in ot['per_second'].items():
                            if values and ttype in otjae_ps_by_run:
                                otjae_ps_by_run[ttype][run_label] = float(np.mean(values))
                for run_label, series in results.get('joularjx_pp', {}).items():
                    if series is not None and not series.empty:
                        jx_pp_by_run[run_label] = float(series.mean())
                for run_label, series in results.get('otjae_pp', {}).items():
                    if series is not None and not series.empty:
                        otjae_pp_by_run[run_label] = float(series.mean())

            # P_T cells: per-repetition mean -> summarize_repetitions (n up to 3)
            tx_stats = {t: {'OTJAE': None, 'JoularJX': None} for t in ttypes}
            for ttype in ttypes:
                tx_stats[ttype]['OTJAE'] = summarize_repetitions(list(otjae_ps_by_run[ttype].values()))
                # Per-second mean of the gap-corrected filtered-methods-power.csv
                # samples, matching the process-level P_P computation
                # (shared._parse_joularjx_result_dir) so P_T and P_P are on the
                # same basis and Sum(P_T) <= P_P holds as expected for a method subset.
                tx_stats[ttype]['JoularJX'] = summarize_repetitions(list(jx_ps_by_run[ttype].values()))

            # Delta = sum(P_T over GET/POST/DELETE) - P_P, computed per-run
            # (session-paired) then summarized, instead of pooling means first.
            otjae_deltas = _session_deltas(otjae_ps_by_run, otjae_pp_by_run)
            jx_deltas = _session_deltas(jx_ps_by_run, jx_pp_by_run)
            session_deltas_by_tool[env_key]['OTJAE'].extend(otjae_deltas)
            session_deltas_by_tool[env_key]['JoularJX'].extend(jx_deltas)
            tx_stats['_delta'] = {'OTJAE': summarize_repetitions(otjae_deltas), 'JoularJX': summarize_repetitions(jx_deltas)}
            tx_stats['_sum'] = {
                'OTJAE': summarize_repetitions(_session_sums(otjae_ps_by_run)),
                'JoularJX': summarize_repetitions(_session_sums(jx_ps_by_run)),
            }

            table_stats[load_level] = tx_stats
        all_env_stats[env_key] = table_stats

    def fmt_tx(stats):
        if stats is None or stats.get("mean") is None:
            return "-"
        return fmt_mean_std(stats["mean"], stats["std"], unit="")

    tool_defs = [("OTJAE", "OTJAE"), ("JoularJX", "JoularJX")]

    # One table per environment; columns: Load | Transaction | (P_T | Δ) per active tool
    for env_key, table_stats in all_env_stats.items():
        sorted_lls = sorted(table_stats.keys(), key=lambda x: int(x))

        # Drop tool columns that have no data at all (across every load
        # level and transaction type) for this environment.
        active_tools = []
        for tool_key, tool_label in tool_defs:
            has_data = any(
                table_stats[ll]['_delta'][tool_key]['mean'] is not None
                or any(table_stats[ll][ttype][tool_key]['mean'] is not None for ttype in ttypes)
                for ll in sorted_lls
            )
            if has_data:
                active_tools.append((tool_key, tool_label))
        n_cols = 2 + 2 * len(active_tools)

        print(f"% {env_key} Environment")
        print(r"""\begin{table*}[!htbp]
    \begin{center}""")
        print(f"        \\caption{{Mean transaction power consumption per second by load level (Environment: {env_key}, Runtime setup: RS1); cells show mean$\\pm$std across n=3 repetitions}}")
        print(f"        \\label{{tab:power_consumption_transaction_{env_key.lower()}}}")
        print(r"""        \resizebox{\textwidth}{!}{%""")
        print("        \\begin{tabular}{ |" + "c|" * n_cols + " }")
        print("            \\hline")
        tool_headers = " & ".join(f"\\multicolumn{{2}}{{|c|}}{{{label}}}" for _, label in active_tools)
        print(f"            \\multirow{{2}}{{*}}{{Load (T/s)}} & \\multirow{{2}}{{*}}{{Transaction}} & {tool_headers} \\\\")
        print("            \\hhline{|~|~|" + "-|" * (2 * len(active_tools)) + "}")
        subheaders = " & ".join("P\\textsubscript{T} (W) & P\\textsubscript{$\\Delta$} (W)" for _ in active_tools)
        print(f"            &  & {subheaders} \\\\")
        print("            \\hline")

        for ll_idx, ll in enumerate(sorted_lls):
            tm = table_stats[ll]
            deltas = tm['_delta']
            lbl = f"{int(ll)}"
            for idx, ttype in enumerate(ttypes):
                load_col = f"\\multirow{{3}}{{*}}{{{lbl}}}" if idx == 0 else ""
                cell_parts = []
                for t, _ in active_tools:
                    pt_cell = fmt_tx(tm[ttype][t])
                    # Δ column spans all 3 transaction rows per load level
                    delta_col = f"\\multirow{{3}}{{*}}{{{fmt_tx(deltas[t])}}}" if idx == 0 else ""
                    cell_parts.append(f"{pt_cell} & {delta_col}")
                tool_cells = " & ".join(cell_parts)
                print(f"            {load_col} & {ttype} & {tool_cells} \\\\")
                if idx < 2:
                    # Partial rule between transaction types: skip load and delta columns
                    partial = "|~|-|" + "-|~|" * len(active_tools)
                    print("            \\hhline{" + partial + "}")
            if ll_idx < len(sorted_lls) - 1:
                print("            \\hhline{|" + "-|" * n_cols + "}")
        print("            \\hline")

        print("        \\end{tabular}")
        print("        }")
        print("    \\end{center}")
        print("\\end{table*}")

    # --- Statistical notes (Sum P_T - P_P internal-consistency Delta vs 0,
    # session-paired across all load levels) ---
    for env_key, tool_deltas in session_deltas_by_tool.items():
        for tool_name, deltas in tool_deltas.items():
            if not deltas:
                continue
            d_paired = cohens_d_paired(deltas)
            wilcoxon = wilcoxon_signed_rank_exact(deltas)
            print(f"% Statistical note ({env_key}, {tool_name} Delta=Sum(P_T)-P_P vs 0), session-paired (n={wilcoxon['n_nonzero']}):")
            if d_paired is not None:
                print(f"%   Cohen's dz = {d_paired:.3f}")
            if wilcoxon["p_two_sided"] is not None:
                print(f"%   Wilcoxon signed-rank: W+={wilcoxon['statistic']:.1f}, p={wilcoxon['p_two_sided']:.4f} (p_floor={wilcoxon['p_floor']:.4f})")

    # --- Sum(P_T) per load level (mean+-std), for reuse as the
    # SCENARIO_CONSTANTS point estimates in create_power_consumption_barchart.py ---
    print("\n=== Sum(P_T) per load level (mean+-std across n=3 repetitions) ===")
    for env_key, table_stats in all_env_stats.items():
        print(f"\n--- Environment: {env_key} ---")
        for load_level in sorted(table_stats.keys(), key=lambda x: int(x)):
            sum_stats = table_stats[load_level].get('_sum', {})
            for tool in ('OTJAE', 'JoularJX'):
                s = sum_stats.get(tool)
                if s and s['mean'] is not None:
                    print(f"  Load {load_level} ({int(load_level) * 3}T/s), {tool}: {fmt_mean_std(s['mean'], s['std'])} (n={s['n']})")


# Run the script if executed directly
if __name__ == "__main__":
    main()
