"""
visualizeLoadLevelContainerPowerConsumptionAsBoxplots.py

Collect container-level power measurements (container metrics like Kepler, Scaphandre or aggregated package-level RAPL) and produce per-load boxplots.

The module contains parsers for specialized HTTP logger metric files as well as helpers to aggregate Rittal and powercap inputs. It can also print LaTeX tables summarizing container power by load.
"""




import re
import json
import os
import pandas as pd
import numpy as np
import matplotlib.pyplot as plt
from pathlib import Path
from collections import defaultdict

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
    filter_application_powerapi_files,
    discover_environments,
    scenario_matches,
    scenario_matches_any,
    get_procfs_glob_pattern,
    get_http_logger_glob_pattern,
    docker_or_vm_endswith,
)


def parse_kepler_http_logger(file_path, service_pids, trim_seconds=0, jmeter_bounds=None):
    """
    Parses a large http_logger_spring_docker_kepler file, extracts kepler_process_cpu_watts for the given service_pids.
    Only includes values within the jmeter_bounds timeframe if provided.
    Returns a DataFrame with columns: ['datetime', 'Power']
    """
    import re
    data = []
    # Regex for DATA line and kepler metric lines
    data_line_re = re.compile(r"^DATA:.* at (\d+)")
    kepler_proc_re = re.compile(r'kepler_process_cpu_watts\{([^}]*)\} ([\d\.eE+-]+)')
    kepler_cont_re = re.compile(r'kepler_container_cpu_watts\{([^}]*)\} ([\d\.eE+-]+)')
    # Helper to parse label string into dict
    def parse_labels(label_str):
        return dict(re.findall(r'(\w+)="([^"]*)"', label_str))
    
    with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
        current_timestamp = None
        proc_metrics = []
        cont_metrics = []
        
        def flush_timestamp():
            if current_timestamp is None or not proc_metrics:
                return
            dt = pd.to_datetime(current_timestamp, unit='ms')
            for labels, value in proc_metrics:
                pid = labels.get('pid')
                container_id = labels.get('container_id')
                if pid in service_pids:
                    if container_id:
                        cont_val = None
                        for clabels, cvalue in cont_metrics:
                            if clabels.get('container_id') == container_id:
                                cont_val = cvalue
                                break
                        if cont_val is not None:
                            data.append({'datetime': dt, 'Power': float(cont_val)})
                        else:
                            data.append({'datetime': dt, 'Power': float(value)})
                    else:
                        data.append({'datetime': dt, 'Power': float(value)})
        
        for line in f:
            m = data_line_re.match(line)
            if m:
                # Flush previous timestamp
                flush_timestamp()
                # Start new timestamp
                current_timestamp = int(m.group(1))
                proc_metrics = []
                cont_metrics = []
            elif current_timestamp is not None:
                km = kepler_proc_re.match(line)
                if km:
                    labels = parse_labels(km.group(1))
                    value = km.group(2)
                    proc_metrics.append((labels, value))
                else:
                    kc = kepler_cont_re.match(line)
                    if kc:
                        labels = parse_labels(kc.group(1))
                        value = kc.group(2)
                        cont_metrics.append((labels, value))
        # Flush last timestamp
        flush_timestamp()
    
    df = pd.DataFrame(data)
    # Optionally trim the time series
    if not df.empty and (trim_seconds > 0 or jmeter_bounds is not None):
        df = trim_time_series(df, trim_seconds, jmeter_bounds)
    return df


def collect_data_by_load_level(trim_seconds=0, scenario_suffixes=None, included_load_levels=None, return_per_env=False):
    """
    Collects and groups all Rittal and Powercap data by load level and optionally environment.
    Only includes scenario subdirectories matching scenario_suffixes if provided.
    If included_load_levels is provided (list of strings), only those load levels are included in the returned data.
    If return_per_env is True, returns a dict: {environment: {load_level: {...}}}
    Otherwise (default), merges all environments: {load_level: {scenario: {...}}}
    """
    exp_results = Path(__file__).resolve().parent
    env_names = discover_environments(exp_results)
    if not env_names:
        env_names = [None]

    if return_per_env:
        data_by_env = {}
        for env_name in env_names:
            env_key = env_name if env_name else 'flat'
            load_level_map = build_run_dirs(exp_results, environment=env_name) if env_name else build_run_dirs(exp_results)
            data_by_load = defaultdict(lambda: defaultdict(lambda: {'rittal': [], 'powercap': [], 'kepler': [], 'scaphandre': [], 'powerapi': []}))

            label = f"[{env_name}] " if env_name else ""

            for load_level, dirs in load_level_map.items():
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
                        kepler_files = list(scenario_dir.glob('**/http_logger_spring_docker_kepler*.csv')) + \
                                       list(scenario_dir.glob('**/http_logger_spring_vm_kepler*.csv'))
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

            data_by_env[env_key] = dict(data_by_load)
        return data_by_env

    # Legacy behavior (merge all environments):
    data_by_load = defaultdict(lambda: defaultdict(lambda: {'rittal': [], 'powercap': [], 'kepler': [], 'scaphandre': [], 'powerapi': []}))
    for env_name in env_names:
        _collect_env_data(exp_results, env_name, trim_seconds, scenario_suffixes, included_load_levels, data_by_load)
    return data_by_load


def _collect_env_data(exp_results, env_name, trim_seconds, scenario_suffixes, included_load_levels, data_by_load):
    """Inner worker: collect data for one environment."""
    load_level_map = build_run_dirs(exp_results, environment=env_name) if env_name else build_run_dirs(exp_results)
    for load_level, dirs in load_level_map.items():
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
                kepler_files = list(scenario_dir.glob('**/http_logger_spring_docker_kepler*.csv')) + \
                               list(scenario_dir.glob('**/http_logger_spring_vm_kepler*.csv'))
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
    return data_by_load


def plot_all_load_levels_in_one_row(data_by_env, output_path, scenario_suffixes=None, custom_labels=None, show_rittal=True):
    """
    Plots a single row of boxplots, one for each load level, for each environment.
    
    data_by_env: {environment: {load_level: {scenario: {...}}}}
    
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
                for scenario in matching:
                    sdata = scenario_dict.get(scenario, {})
                    all_rittal.extend(sdata.get('rittal', []))
                    all_powercap.extend(sdata.get('powercap', []))
                    all_kepler.extend(sdata.get('kepler', []))
                    all_scaphandre.extend(sdata.get('scaphandre', []))
                    all_powerapi.extend(sdata.get('powerapi', []))
                if show_rittal and all_rittal:
                    combined_rittal = pd.concat(all_rittal, ignore_index=True)
                    #label = custom_labels.get(suffix, suffix) if custom_labels else suffix
                    box_data.append(combined_rittal)
                    box_labels.append(f'$P_{{EM}}$')
                if all_powercap:
                    combined_powercap = pd.concat(all_powercap, ignore_index=True)
                    #label = custom_labels.get(suffix, suffix) if custom_labels else suffix
                    box_data.append(combined_powercap)
                    box_labels.append(f'$P_{{S}}$')
                if all_kepler:
                    combined_kepler = pd.concat(all_kepler, ignore_index=True)
                    #label = custom_labels.get(suffix, suffix) if custom_labels else suffix
                    box_data.append(combined_kepler)
                    box_labels.append(f'$P_{{C_{{K}}}}$')
                if all_scaphandre:
                    combined_scaphandre = pd.concat(all_scaphandre, ignore_index=True)
                    #label = custom_labels.get(suffix, suffix) if custom_labels else suffix
                    box_data.append(combined_scaphandre)
                    box_labels.append(f'$P_{{C_{{Sc}}}}$')
                if all_powerapi:
                    combined_powerapi = pd.concat(all_powerapi, ignore_index=True)
                    #label = custom_labels.get(suffix, suffix) if custom_labels else suffix
                    box_data.append(combined_powerapi)
                    box_labels.append(f'$P_{{C_{{PA}}}}$')
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
    ]
    custom_labels = {
        "idle_no_tools": "Idle",
        "docker_tools": "",
        "docker_none": "Container\nIdle (CI)",
        "docker_idle": "CI, Powercap (PC)\nand ProcFS",
        "docker_kepler": "",
        "docker_scaphandre": "",
        "docker_powerapi": "",
    }
    included_load_levels = ["230", "350", "480", "560"]
    show_rittal = False

    exp_results = Path(__file__).resolve().parent
    env_names = discover_environments(exp_results)
    if not env_names:
        env_names = [None]

    # Collect environment data for combined table
    env_data_store = {}

    for env_name in env_names:
        env_key = env_name if env_name else 'flat'

        data_by_env = collect_data_by_load_level(
            trim_seconds=trim_seconds,
            scenario_suffixes=scenario_suffixes,
            included_load_levels=included_load_levels,
            return_per_env=True,
        )

        env_data = data_by_env.get(env_key, {})
        if not env_data:
            print(f"No data for environment {env_name}")
            continue

        env_output = f"./container_power_consumption_boxplots_{env_key}_all_loads.pdf"
        plot_all_load_levels_in_one_row(data_by_env, env_output, scenario_suffixes=scenario_suffixes, custom_labels=custom_labels, show_rittal=show_rittal)
        print(f"Environment {env_key}: Saved plot to {env_output}")

        # Store for LaTeX table generation
        env_data_store[env_key] = env_data

    # --- Combined LaTeX Table for all environments ---
    print("\\begin{table}")
    print("    \\centering")
    print("    \\resizebox{\\textwidth}{!}{%")
    print("        \\begin{tabular}{ |c|c|c|c|c|c|c|c|c| }")
    print("            \\hline")
    print("            \\multirow{2}{*}{Environment} & \\multirow{2}{*}{Load} & \\multirow{2}{*}{P\\textsubscript{S}}  & \\multicolumn{2}{|c|}{Kepler}  & \\multicolumn{2}{|c|}{Scaphandre} & \\multicolumn{2}{|c|}{{PowerAPI}} \\\\")
    print("            \\hhline{|~|~|~|-|-|-|-|-|-|}")
    print("            &           &  & P\\textsubscript{C} &  $\\Delta$ & P\\textsubscript{C} &  $\\Delta$ & P\\textsubscript{C} &  $\\Delta$ \\\\")
    print("            \\hline")

    def get_mean_power(scenario_key, subkey, sd):
        vals = []
        for k in sd:
            if k.endswith(scenario_key):
                vals.extend(sd[k][subkey])
        if vals:
            combined = pd.concat(vals, ignore_index=True)
            return float(combined.mean())
        return None

    def pct(val, ref):
        if val is None or ref is None or ref == 0:
            return "-"
        return f"{(val / ref * 100):.2f}\\%"

    def fmt(val):
        return f"{val:.2f}W" if val is not None else "-"

    for env_key, env_data in env_data_store.items():
        numeric_items = [(k, v) for k, v in env_data.items() if k.isdigit()]
        sorted_loads = sorted(numeric_items, key=lambda x: int(x[0]))

        for load_level, scenario_dict in sorted_loads:
            Ps = get_mean_power('tools', 'powercap', scenario_dict)
            Pkc = get_mean_power('kepler', 'kepler', scenario_dict)
            Psc = get_mean_power('scaphandre', 'scaphandre', scenario_dict)
            Ppa = get_mean_power('powerapi', 'powerapi', scenario_dict)
            load_label = f"{int(load_level)*3}T/s"
            env_label = f"{env_key} (RS1)" if "(RS" not in env_key else env_key
            print(f"            {env_label} & {load_label} & {fmt(Ps)}  & {fmt(Pkc)}  & {pct(Pkc, Ps)} & {fmt(Psc)} & {pct(Psc, Ps)} & {fmt(Ppa)} & {pct(Ppa, Ps)} \\\\")
            print("            \\hline")

    print("        \\end{tabular}")
    print("    }")
    print("    \\caption{Mean container power consumption by environment and load level}")
    print("    \\label{tab:container_power_depending_throughput}")
    print("\\end{table}")


# Run the script if executed directly
if __name__ == "__main__":
    main()
