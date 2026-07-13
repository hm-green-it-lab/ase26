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
import os
import re
import json
import threading

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
    discover_environments,
    scenario_matches,
    scenario_matches_any,
    get_procfs_glob_pattern,
    get_http_logger_glob_pattern,
    docker_or_vm_endswith,
)

def calculate_otjae_transaction_power_cpu(scenario_dir, procfs_file, service_pids, otjae_per_second, jmeter_bounds=None, n_cores=80, ticks_per_sec=100, data_by_load=None):
    """
    Calculate per-transaction power for OTJAE using CPU time fraction per second and process power from procfs.
    otjae_per_second: output of parse_otjae_transaction_resource (per-second resource demand)
    Returns: dict {transaction: [P_transaction_per_sec, ...]}
    """
    # Use process_docker_otjae to get process power per second (includes CPU, memory, storage)
    # We need pcpumin and pcpumax, but for this function, we assume they are available from the main data collection (they can be passed as args if needed)
    # For now, try to extract them from the global data_by_load if available, else fallback to None
    pcpumin = data_by_load.get('pcpumin', None)
    pcpumax = data_by_load.get('pcpumax', None)
    if pcpumin is None or pcpumax is None:
        # fallback: try to estimate from process power min/max
        pcpumin = 0
        pcpumax = 1
    # Get process power time series (indexed by datetime)
    process_power_series = process_docker_otjae(scenario_dir, 0, pcpumin, pcpumax)
    if process_power_series is None or process_power_series.empty:
        return {}
    # Build mapping from second (int) to process power (float)
    process_power_sec = {}
    for sec, val in process_power_series.items():
        process_power_sec[sec] = val

    # Harmonize seconds: find the minimum second in otjae_per_second and cpu_time_per_sec, shift all to start from 0
    otjae_seconds = list(otjae_per_second.keys())
    # Get process CPU time per second (from sys_df)
    proc_util, sys_df, _, _ = parse_procfs_data(procfs_file, service_pids, n_cores=n_cores, ticks_per_sec=ticks_per_sec, jmeter_bounds=jmeter_bounds)
    if sys_df is None or sys_df.empty:
        return {}
    sys_df = sys_df.copy()
    sys_df['sec'] = (sys_df['datetime'].astype('int64') // 10**9).astype(int)
    cpu_time_per_sec = sys_df.groupby('sec')['delta_cpu'].sum()
    cpu_seconds = list(cpu_time_per_sec.index)
    # Find the minimum second across both otjae_per_second and cpu_time_per_sec
    all_seconds = otjae_seconds + cpu_seconds
    if not all_seconds:
        return {}
    min_sec = min(all_seconds)
    # Shift otjae_per_second keys
    otjae_per_second_rel = {sec - min_sec: txs for sec, txs in otjae_per_second.items()}
    # Shift cpu_time_per_sec keys
    cpu_time_per_sec_rel = {sec - min_sec: val for sec, val in cpu_time_per_sec.items()}

    # Build per-transaction power per second, normalized by number of invocations and also total per second
    tx_power_per_invocation = {}
    tx_power_per_second = {}
    for sec, txs in otjae_per_second_rel.items():
        proc_cpu = cpu_time_per_sec_rel.get(sec, None)
        P_proc = process_power_sec.get(sec, None)
        if proc_cpu is None or proc_cpu == 0 or P_proc is None:
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
            if proc_cpu > 0:
                P_tx_cpu_total = (cpu_tx_sec / proc_cpu) * P_proc
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
    import pandas as pd
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

def ensure_joularjx_unzipped(scenario_dir):
    """
    For each joularjx-result_*.zip found under scenario_dir, extract it into the
    same parent directory if the corresponding directory does not yet exist or
    contains no subdirectories (i.e., the results were never unzipped).
    """
    import zipfile
    for zip_path in Path(scenario_dir).glob('**/joularjx-result_*.zip'):
        target_dir = zip_path.parent / zip_path.stem
        needs_extract = (
            not target_dir.exists()
            or not any(p.is_dir() for p in target_dir.iterdir())
        )
        if needs_extract:
            print(f"Extracting {zip_path} ...")
            try:
                with zipfile.ZipFile(str(zip_path), 'r') as zf:
                    zf.extractall(str(zip_path.parent))
                print(f"Extracted {zip_path.name}")
            except Exception as e:
                print(f"Error extracting {zip_path}: {e}")


def parse_joularjx_transaction_power(methods_dir, jmeter_bounds=None):
    """
    Parse all JoularJX methods-power.csv files in a directory, only using files whose timestamp is within jmeter_bounds.
    Returns a dict: {'per_invocation': {transaction_type: [...]}, 'per_second': {transaction_type: [...]}, 'total_energy': {transaction_type: total_energy}, 'steady_state_time': steady_state_time}
    """
    import re
    from collections import defaultdict
    import pandas as pd
    if not Path(methods_dir).is_dir():
        return {}

    # --- Validation: Read total energy file if available ---
    total_methods_dir = Path(methods_dir).parent.parent / "total" / "methods"
    total_energy_file = total_methods_dir / "joularJX-1-filtered-methods-energy.csv"
    total_energy_from_file = {}
    if total_energy_file.exists():
        try:
            import csv
            with open(total_energy_file, "r", encoding="utf-8") as f:
                reader = csv.reader(f)
                for row in reader:
                    if len(row) == 2:
                        method, energy = row
                        if method.endswith(".getData"):
                            total_energy_from_file["GET"] = float(energy)
                        elif method.endswith(".postData"):
                            total_energy_from_file["POST"] = float(energy)
                        elif method.endswith(".deleteData"):
                            total_energy_from_file["DELETE"] = float(energy)
        except Exception as e:
            print(f"[JoularJX Validation] Error reading total energy file: {e}")
    # Get bounds in ms since epoch
    if jmeter_bounds is not None and all(jmeter_bounds):
        start, end = jmeter_bounds
        start_ms = int(start.value // 10**6)
        end_ms = int(end.value // 10**6)
    else:
        start_ms = end_ms = None
    # Find all files matching pattern
    files = list(Path(methods_dir).glob('joularJX-*-filtered-methods-power.csv'))
    # Extract timestamp from filename
    file_tuples = []
    for f in files:
        m = re.search(r'joularJX-\d+-(\d+)-filtered-methods-power.csv', f.name)
        if m:
            ts = int(m.group(1))
            file_tuples.append((ts, f))
    # Filter files to steady-state
    if start_ms is not None and end_ms is not None:
        file_tuples = [(ts, f) for ts, f in file_tuples if start_ms <= ts <= end_ms]
    # For each file, calculate power per transaction type using time delta
    per_invocation = defaultdict(list)
    per_second = defaultdict(list)
    total_energy = defaultdict(float)
    prev_ts = None
    steady_state_start = None
    steady_state_end = None
    sorted_files = sorted(file_tuples)
    for idx, (ts, f) in enumerate(sorted_files):
        if steady_state_start is None or ts < steady_state_start:
            steady_state_start = ts
        if steady_state_end is None or ts > steady_state_end:
            steady_state_end = ts
        df = pd.read_csv(f, header=None, names=['method', 'energy'])
        # Determine time delta to previous file (in seconds)
        if prev_ts is not None:
            delta_t = (ts - prev_ts) / 1000.0  # timestamps are in ms
        else:
            # For the first file, if only one file, set delta_t=1, else use next file's delta
            if len(sorted_files) > 1 and idx+1 < len(sorted_files):
                next_ts = sorted_files[idx+1][0]
                delta_t = (next_ts - ts) / 1000.0
            else:
                delta_t = 1.
        #print(f"Processing file: {f}, ts: {ts}, delta_t: {delta_t}")        
        prev_ts = ts
        for ttype in ['getData', 'postData', 'deleteData']:
            mask = df['method'].str.endswith(f'.{ttype}')
            energy_sum = df.loc[mask, 'energy'].sum()
            num_invocations = mask.sum()
            # Power per second for this transaction type (all invocations in this second)
            if delta_t > 0:
                power_per_second = energy_sum / delta_t
            else:
                power_per_second = 0
            per_second[ttype.upper().replace('DATA','')].append(power_per_second)
            # Power per invocation (for boxplots)
            if delta_t > 0 and num_invocations > 0:
                power_per_invocation = (energy_sum / delta_t) / num_invocations
            else:
                power_per_invocation = 0
            per_invocation[ttype.upper().replace('DATA','')].append(power_per_invocation)
            # Sum total energy for this transaction type
            total_energy[ttype.upper().replace('DATA','')] += energy_sum

    # --- Validation: Compare calculated total_energy with file values and print results ---
    if total_energy_from_file:
        print("[JoularJX Validation] Comparing calculated total_energy with total file values:")
        for ttype in ['GET', 'POST', 'DELETE']:
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
    else:
        print("[JoularJX Validation] No total energy file found for validation.")
    # Calculate steady-state time in seconds
    if steady_state_start is not None and steady_state_end is not None and steady_state_end > steady_state_start:
        steady_state_time = (steady_state_end - steady_state_start) / 1000.0
    else:
        steady_state_time = None
    return {
        'per_invocation': per_invocation,
        'per_second': per_second,
        'total_energy': total_energy,
        'steady_state_time': steady_state_time,
        'total_energy_from_file': total_energy_from_file
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
    # Find procfs file - match both spring_docker and spring_vm patterns
    tool_suffix = scenario_dir.name.split('_')[-1]
    procfs_files = list(scenario_dir.glob(f'**/procfs_*_{tool_suffix}*.csv'))
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
    Collects and groups all Rittal and Powercap data by environment and load level.
    Aggregates all runs (e.g., 350, 350_run2, 350_run3) for each load level within each environment.
    Only includes scenario subdirectories matching scenario_suffixes if provided.
    If included_load_levels is provided (list of strings), only those load levels are included in the returned data (except for pcpumin/pcpumax, which always use all data).
    Returns a dict: {environment: {load_level: {scenario: {...}}, ...}}
    """
    exp_results = Path(__file__).resolve().parent
    env_names = discover_environments(exp_results)
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
            env_prefix = f'{env_name}: ' if env_name and env_name != 'flat' else ''
            for scenario in scenario_dict:
                sdata = scenario_dict[scenario]
                if 'joularjx_tx' in sdata:
                    joularjx_tx = sdata['joularjx_tx']
                    if isinstance(joularjx_tx, dict) and BOXPLOT_DATA_MODE in joularjx_tx:
                        tx_dict = joularjx_tx[BOXPLOT_DATA_MODE]
                    elif isinstance(joularjx_tx, dict) and 'per_invocation' in joularjx_tx:
                        tx_dict = joularjx_tx['per_invocation']
                    else:
                        tx_dict = joularjx_tx
                    for ttype in ['GET', 'POST', 'DELETE']:
                        if ttype in tx_dict and tx_dict[ttype]:
                            tx_agg_jx[ttype].extend(tx_dict[ttype])
                if 'otjae_tx' in sdata:
                    otjae_tx = sdata['otjae_tx']
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
                    box_labels.append(f'{env_prefix}${ttype}_{{J}}$')
                if tx_agg_ot[ttype]:
                    box_data.append(pd.Series(tx_agg_ot[ttype]))
                    box_labels.append(f'{env_prefix}${ttype}_{{O}}$')
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
    env_names = discover_environments(exp_results)
    if not env_names:
        env_names = [None]

    env_data_store = {}

    for env_name in env_names:
        env_key = env_name if env_name else 'flat'
        load_level_map = build_run_dirs(exp_results, environment=env_name) if env_name else build_run_dirs(exp_results)
        data_by_load = defaultdict(lambda: defaultdict(lambda: {'rittal': [], 'powercap': [], 'joularjx_tx': {}, 'otjae_tx': {}}))

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
        from threading import Lock
        data_lock = Lock()

        def process_scenario(load_level, scenario_dir):
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
                            data_by_load[load_level][scenario_name]['joularjx_tx'] = tx_power
            if scenario_matches(scenario_name, 'tools'):
                rfs = list(scenario_dir.glob('**/rittal_*.csv'))
                pcf = list(scenario_dir.glob('**/powercap_*.csv'))
                for fp in rfs:
                    try:
                        pd2 = load_rittal_data(str(fp), trim_seconds, jb)
                        if 'Power (Watts)' in pd2.columns:
                            with data_lock:
                                data_by_load[load_level][scenario_name]['rittal'].append(pd2['Power (Watts)'])
                    except Exception as e:
                        pass
                for fp in pcf:
                    try:
                        pd2 = load_power_data(str(fp), trim_seconds, jb)
                        if 'Power' in pd2.columns:
                            with data_lock:
                                data_by_load[load_level][scenario_name]['powercap'].append(pd2['Power'])
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
                                data_by_load[load_level][scenario_name]['otjae_tx'] = tx_power

        threads = []
        for load_level, dirs in load_level_map.items():
            if included_load_levels is not None and load_level not in included_load_levels:
                continue
            for run_path in dirs:
                for scenario_dir in run_path.iterdir():
                    if not scenario_dir.is_dir():
                        continue
                    t = threading.Thread(target=process_scenario, args=(load_level, scenario_dir))
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
                if 'joularjx_tx' in results and results['joularjx_tx']:
                    joularjx_tx = results['joularjx_tx']
                    ss_time = joularjx_tx.get('steady_state_time', None)
                    teff = joularjx_tx.get('total_energy_from_file', {})
                    if isinstance(joularjx_tx, dict) and 'per_invocation' in joularjx_tx and 'per_second' in joularjx_tx:
                        for ttype in ['GET', 'POST', 'DELETE']:
                            pi = joularjx_tx['per_invocation'].get(ttype, [])
                            ps = joularjx_tx['per_second'].get(ttype, [])
                            te = joularjx_tx.get('total_energy', {}).get(ttype, None)
                            if pi:
                                print(f"  {scenario} - {ttype}_J: mean per-invocation = {float(np.mean(pi)):.3f} W", end='')
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
                                print(f"  {scenario} - {ttype}_J: mean = {float(np.mean(values)):.3f} W")
                if 'otjae_tx' in results and results['otjae_tx']:
                    otjae_tx = results['otjae_tx']
                    if isinstance(otjae_tx, dict) and 'per_invocation' in otjae_tx and 'per_second' in otjae_tx:
                        for ttype in ['GET', 'POST', 'DELETE']:
                            pi = otjae_tx['per_invocation'].get(ttype, [])
                            ps = otjae_tx['per_second'].get(ttype, [])
                            if pi:
                                print(f"  {scenario} - {ttype}_O: mean per-invocation = {float(np.mean(pi)):.3f} W", end='')
                            if ps:
                                print(f", mean per-second = {float(np.mean(ps)):.3f} W")
                            elif pi:
                                print()
                    else:
                        for ttype, values in otjae_tx.items():
                            if values:
                                print(f"  {scenario} - {ttype}_O: mean = {float(np.mean(values)):.3f} W")

    # --- Combined LaTeX Table (All Environments) ---
    print("\n=== Combined LaTeX Table (All Environments) ===")

    # Collect table_means for every environment first
    all_env_means = {}
    for env_key, data_by_env in env_data_store.items():
        table_means = {}
        for load_level, scenario_dict in data_by_env.items():
            if not str(load_level).isdigit():
                continue
            tx_means = {'GET': {'OTJAE': None, 'JoularJX': None, 'JoularJX_total': None, 'JoularJX_file_total': None},
                        'POST': {'OTJAE': None, 'JoularJX': None, 'JoularJX_total': None, 'JoularJX_file_total': None},
                        'DELETE': {'OTJAE': None, 'JoularJX': None, 'JoularJX_total': None, 'JoularJX_file_total': None}}
            agg_otjae_ps = {'GET': [], 'POST': [], 'DELETE': []}
            agg_jx_ps = {'GET': [], 'POST': [], 'DELETE': []}
            agg_jx_te = {'GET': 0.0, 'POST': 0.0, 'DELETE': 0.0}
            agg_jx_fte = {'GET': [], 'POST': [], 'DELETE': []}
            agg_jx_tst = []
            for scenario, results in scenario_dict.items():
                if 'joularjx_tx' in results and results['joularjx_tx']:
                    jx = results['joularjx_tx']
                    if isinstance(jx, dict) and 'per_second' in jx:
                        for ttype, values in jx['per_second'].items():
                            if values:
                                agg_jx_ps[ttype].extend(values)
                        stt = jx.get('total_energy', {})
                        fte = jx.get('total_energy_from_file', {})
                        sst = jx.get('steady_state_time', None)
                        if sst and sst > 0:
                            agg_jx_tst.append(sst)
                            for ttype in ['GET', 'POST', 'DELETE']:
                                agg_jx_te[ttype] += stt.get(ttype, 0.0)
                                val = fte.get(ttype, None)
                                if val is not None:
                                    agg_jx_fte[ttype].append(val)
                if 'otjae_tx' in results and results['otjae_tx']:
                    ot = results['otjae_tx']
                    if isinstance(ot, dict) and 'per_second' in ot:
                        for ttype, values in ot['per_second'].items():
                            if values:
                                agg_otjae_ps[ttype].extend(values)
            mean_sst = np.mean(agg_jx_tst) if agg_jx_tst else None
            for ttype in ['GET', 'POST', 'DELETE']:
                if agg_otjae_ps[ttype]:
                    tx_means[ttype]['OTJAE'] = float(np.mean(agg_otjae_ps[ttype]))
                if agg_jx_ps[ttype]:
                    tx_means[ttype]['JoularJX'] = float(np.mean(agg_jx_ps[ttype]))
                if mean_sst and mean_sst > 0:
                    tx_means[ttype]['JoularJX_total'] = agg_jx_te[ttype] / mean_sst
                    ett = mean_sst + 2 * trim_seconds
                else:
                    tx_means[ttype]['JoularJX_total'] = None
                    ett = None
                if agg_jx_fte[ttype] and ett and ett > 0:
                    tx_means[ttype]['JoularJX_file_total'] = np.mean(agg_jx_fte[ttype]) / ett
                else:
                    tx_means[ttype]['JoularJX_file_total'] = None
            table_means[load_level] = tx_means
        all_env_means[env_key] = table_means

    def fmt_tx(val):
        return f"{val:.2f}W" if val is not None else "-"

    # 7 columns: Environment | Load | Transaction | OTJAE P_T | OTJAE Δ | JoularJX P_T | JoularJX Δ
    print(r"""\begin{table*}[h]
    \begin{center}
        \begin{tabular}{ |c|c|c|c|c|c|c| }
            \hline
            \multirow{2}{*}{Environment} & \multirow{2}{*}{Load} & \multirow{2}{*}{Transaction} & \multicolumn{2}{|c|}{OTJAE} & \multicolumn{2}{|c|}{JoularJX} \\
            \hhline{|~|~|~|-|-|-|-|}
            &  &  & P\textsubscript{T} & $\Delta$ & P\textsubscript{T} & $\Delta$ \\
            \hline""")

    for env_key, table_means in all_env_means.items():
        sorted_lls = sorted(table_means.keys(), key=lambda x: int(x))
        n_env_rows = len(sorted_lls) * 3
        env_label = f"{env_key} (RS1)" if "(RS" not in env_key else env_key
        for ll_idx, ll in enumerate(sorted_lls):
            tm = table_means[ll]
            lbl = f"{int(ll)}T/s"
            for idx, ttype in enumerate(['GET', 'POST', 'DELETE']):
                ov = tm[ttype]['OTJAE']
                jftv = tm[ttype]['JoularJX_file_total']
                ovs = fmt_tx(ov)
                jfts = fmt_tx(jftv)
                env_col = f"\\multirow{{{n_env_rows}}}{{*}}{{{env_label}}}" if ll_idx == 0 and idx == 0 else ""
                load_col = f"\\multirow{{3}}{{*}}{{{lbl}}}" if idx == 0 else ""
                # Δ columns span all 3 transaction rows per load level
                delta_col = "\\multirow{3}{*}{-}" if idx == 0 else ""
                print(f"            {env_col} & {load_col} & {ttype} & {ovs} & {delta_col} & {jfts} & {delta_col} \\\\")
                if idx < 2:
                    # Partial rule between transaction types: skip env, load and delta columns
                    print("            \\hhline{|~|~|-|-|~|-|~|}")
            if ll_idx < len(sorted_lls) - 1:
                # Partial rule between load levels: skip only the environment column
                print("            \\hhline{|~|-|-|-|-|-|-|}")
        # Full rule after all load levels for this environment
        print("            \\hline")

    print(r"""        \end{tabular}
        \caption{Mean transaction power consumption per second by environment and load level}
        \label{tab:power_consumption_transaction}
    \end{center}
\end{table*}""")


# Run the script if executed directly
if __name__ == "__main__":
    main()
