"""
visualizeLoadLevelContainerPowerConsumptionAsBoxplots.py

Collect container-level power measurements (container metrics like Kepler, Scaphandre or aggregated package-level RAPL) and produce per-load boxplots.

The module contains parsers for specialized HTTP logger metric files as well as helpers to aggregate Rittal and powercap inputs. It can also print LaTeX tables summarizing container power by load.
"""




import argparse
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


def collect_data_by_load_level(trim_seconds=0, scenario_suffixes=None, included_load_levels=None, return_per_env=False, environments=None):
    """
    Collects and groups all Rittal and Powercap data by load level and optionally environment.
    Only includes scenario subdirectories matching scenario_suffixes if provided.
    If included_load_levels is provided (list of strings), only those load levels are included in the returned data.
    If return_per_env is True, returns a dict: {environment: {load_level: {...}}}
    Otherwise (default), merges all environments: {load_level: {scenario: {...}}}
    If environments is provided (list of strings), only those environments are processed.
    """
    exp_results = Path(__file__).resolve().parent
    env_names = [e for e in discover_environments(exp_results) if e in ("Container", "VM")]
    if not env_names:
        env_names = [None]
    if environments is not None:
        env_names = [env_name for env_name in env_names if env_name in environments]

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
                    run_label = run_path.name
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
                                        data_by_load[load_level][scenario_name]['rittal'].append((run_label, power_data['Power (Watts)']))
                                except Exception as e:
                                    print(f"Error loading {file_path}: {e}")
                            for file_path in powercap_files:
                                try:
                                    power_data = load_power_data(str(file_path), trim_seconds, jmeter_bounds)
                                    if 'Power' in power_data.columns:
                                        data_by_load[load_level][scenario_name]['powercap'].append((run_label, power_data['Power']))
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
                                        data_by_load[load_level][scenario_name]['kepler'].append((run_label, kepler_data['Power']))
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
                                        data_by_load[load_level][scenario_name]['scaphandre'].append((run_label, scaphandre_data['Power']))
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
                                        data_by_load[load_level][scenario_name]['powerapi'].append((run_label, powerapi_data['Power']))
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
                    all_rittal.extend(series for _run_label, series in sdata.get('rittal', []))
                    all_powercap.extend(series for _run_label, series in sdata.get('powercap', []))
                    all_kepler.extend(series for _run_label, series in sdata.get('kepler', []))
                    all_scaphandre.extend(series for _run_label, series in sdata.get('scaphandre', []))
                    all_powerapi.extend(series for _run_label, series in sdata.get('powerapi', []))
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


def parse_args():
    parser = argparse.ArgumentParser(
        description="Generate container power consumption boxplots by load level."
    )
    parser.add_argument(
        "--environment",
        nargs="+",
        default=None,
        help="One or more environment names to process (e.g. Container VM). When omitted, all discovered environments are processed.",
    )
    return parser.parse_args()


def main():
    """
    Main entry point: collects data, generates and saves the boxplot PDF for all load levels.
    Handles results per environment (Container, VM, etc.) separately.
    """
    args = parse_args()

    trim_seconds = 60
    scenario_suffixes = [
        "tools",
        "kepler",
        "scaphandre",
        "powerapi",
    ]
    custom_labels = {
        "idle_no_tools": "Idle",
        "docker_tools": "CI, Powercap (PC)\nand ProcFS",
        "docker_none": "Container\nIdle (CI)",
        "docker_kepler": "",
        "docker_scaphandre": "",
        "docker_powerapi": "",
    }
    included_load_levels = ["230", "350", "480", "560"]
    show_rittal = False

    exp_results = Path(__file__).resolve().parent
    env_names = [e for e in discover_environments(exp_results) if e in ("Container", "VM")]
    if not env_names:
        env_names = [None]

    if args.environment is not None:
        unknown = [e for e in args.environment if e not in env_names]
        for e in unknown:
            print(f"Warning: environment '{e}' not found, skipping.")
        env_names = [e for e in env_names if e in args.environment]
        if not env_names:
            print("No matching environments to process.")
            return

    # Collect environment data for combined table
    env_data_store = {}

    data_by_env = collect_data_by_load_level(
        trim_seconds=trim_seconds,
        scenario_suffixes=scenario_suffixes,
        included_load_levels=included_load_levels,
        return_per_env=True,
        environments=env_names,
    )

    for env_name in env_names:
        env_key = env_name if env_name else 'flat'

        env_data = data_by_env.get(env_key, {})
        if not env_data:
            print(f"No data for environment {env_name}")
            continue

        env_output = f"./container_power_consumption_boxplots_{env_key}_all_loads.pdf"
        plot_all_load_levels_in_one_row({env_key: env_data}, env_output, scenario_suffixes=scenario_suffixes, custom_labels=custom_labels, show_rittal=show_rittal)
        print(f"Environment {env_key}: Saved plot to {env_output}")

        # Store for LaTeX table generation
        env_data_store[env_key] = env_data

    # --- One LaTeX table per environment ---
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
    pooled_relative_error_by_tool = {
        env_key: {tool: [] for tool in ("kepler", "scaphandre", "powerapi")}
        for env_key in env_data_store
    }

    tool_defs = [("kepler", "Kepler"), ("scaphandre", "Scaphandre"), ("powerapi", "PowerAPI")]

    for env_key, env_data in env_data_store.items():
        numeric_items = [(k, v) for k, v in env_data.items() if k.isdigit()]
        sorted_loads = sorted(numeric_items, key=lambda x: int(x[0]))
        n_rows = len(sorted_loads)

        # Precompute every row first so tool columns with no data at all
        # (across every load level) can be dropped from the table entirely.
        rows = []
        for load_level, scenario_dict in sorted_loads:
            ps_by_run = per_run_means(scenario_dict, 'tools', 'powercap')
            tool_by_run = {t: per_run_means(scenario_dict, t, t) for t, _ in tool_defs}

            ps_stats = summarize_repetitions(list(ps_by_run.values()))
            ps_mean_this_level = ps_stats["mean"]
            for t, _ in tool_defs:
                if ps_mean_this_level:
                    pooled_relative_error_by_tool[env_key][t].extend(
                        (v - ps_mean_this_level) / ps_mean_this_level for v in tool_by_run[t].values()
                    )
            tool_stats = {t: summarize_repetitions(list(tool_by_run[t].values())) for t, _ in tool_defs}
            rows.append({
                "load_label": f"{int(load_level)*3}",
                "ps_cell": fmt_mean_std(ps_stats["mean"], ps_stats["std"], unit=""),
                "tool_cells": {t: fmt_mean_std(tool_stats[t]["mean"], tool_stats[t]["std"], unit="") for t, _ in tool_defs},
                "tool_pct_cells": {t: fmt_pct(pct_stats(tool_by_run[t], ps_by_run)) for t, _ in tool_defs},
            })

        active_tools = [(t, label) for t, label in tool_defs if any(r["tool_cells"][t] != "-" for r in rows)]
        n_cols = 2 + 2 * len(active_tools)

        print(f"% {env_key} Environment")
        print("\\begin{table*}")
        print("    \\centering")
        print(f"    \\caption{{Mean container power consumption by load level (Environment: {env_key}, Runtime setup: RS1); cells show mean$\\pm$std across n=3 repetitions}}")
        print(f"    \\label{{tab:container_power_depending_throughput_{env_key.lower()}}}")
        print("    \\resizebox{\\textwidth}{!}{%")
        print("        \\begin{tabular}{ |" + "c|" * n_cols + " }")
        print("            \\hline")
        tool_headers = "  & ".join(f"\\multicolumn{{2}}{{|c|}}{{{label}}}" for _, label in active_tools)
        print(f"            \\multirow{{2}}{{*}}{{Load (T/s)}} & \\multirow{{2}}{{*}}{{P\\textsubscript{{S}} (W)}}  & {tool_headers} \\\\")
        print("            \\hhline{|~|~|" + "-|" * (2 * len(active_tools)) + "}")
        subheaders = " & ".join("P\\textsubscript{C} (W) &  $\\Delta$ (\\%)" for _ in active_tools)
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
