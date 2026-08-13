"""
visualizeLoadLevelSystemPowerConsumptionAsBoxplots.py

Aggregate system-level power (EM/Rittal and RAPL) across experimental runs and produce boxplots per load level. Also prints a LaTeX table summarizing system power and CPU utilization.
"""

# Import required libraries
import pandas as pd
import numpy as np
import matplotlib.pyplot as plt
from pathlib import Path
import glob
from collections import defaultdict

from shared import (
    build_run_dirs, get_jmeter_time_bounds, extract_service_pids, trim_time_series,
    load_rittal_data, calculate_power_from_energy, load_power_data,
    summarize_repetitions, cohens_d_paired, wilcoxon_signed_rank_exact, fmt_mean_std,
    per_run_means,
)


def collect_data_by_load_level(trim_seconds=0, scenario_suffixes=None):
    """
    Collects and groups all Rittal and Powercap data by environment and load level.
    Aggregates all runs (e.g., 350, 350_run2, 350_run3) for each load level within each environment.
    Only includes scenario subdirectories matching scenario_suffixes if provided.
    Returns a dict: {environment: {load_level: {scenario: {'rittal': [series...], 'powercap': [series...]}}}}
    """
    from shared import discover_environments

    exp_results = Path(__file__).resolve().parent
    env_names = discover_environments(exp_results)
    if not env_names:
        env_names = [None]

    # Outer structure: data_by_env[environment][load_level][scenario] = {rittal: [...], powercap: [...]}
    data_by_env = {}

    for env_name in env_names:
        load_level_map = build_run_dirs(exp_results, environment=env_name) if env_name else build_run_dirs(exp_results)
        data_by_load = defaultdict(lambda: defaultdict(lambda: {'rittal': [], 'powercap': []}))

        label = f"[{env_name}] " if env_name else ""

        for load_level, dirs in load_level_map.items():
            for run_path in dirs:
                for scenario_dir in run_path.iterdir():
                    if not scenario_dir.is_dir():
                        continue
                    scenario_name = scenario_dir.name
                    if scenario_suffixes is not None and not any(scenario_name.endswith(suf) for suf in scenario_suffixes):
                        continue
                    jmeter_bounds = get_jmeter_time_bounds(str(scenario_dir), trim_seconds)
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

        env_key = env_name if env_name else 'flat'
        data_by_env[env_key] = dict(data_by_load)

    return data_by_env


def plot_all_load_levels_in_one_row(data_by_env, output_path, scenario_suffixes=None, custom_labels=None):
    """
    Plots a single row of boxplots, one for each load level, for each environment.
    
    data_by_env: {environment: {load_level: {scenario: {'rittal': [...], 'powercap': [...]}}}}
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
                for scenario in matching:
                    sdata = scenario_dict.get(scenario, {})
                    all_rittal.extend(series for _run_label, series in sdata.get('rittal', []))
                    all_powercap.extend(series for _run_label, series in sdata.get('powercap', []))
                if all_rittal:
                    combined = pd.concat(all_rittal, ignore_index=True)
                    label = custom_labels.get(suffix, suffix) if custom_labels else suffix
                    box_data.append(combined)
                    box_labels.append(f'{label}$P_{{EM}}$')
                if all_powercap:
                    combined = pd.concat(all_powercap, ignore_index=True)
                    label = custom_labels.get(suffix, suffix) if custom_labels else suffix
                    box_data.append(combined)
                    box_labels.append(f'{label}$P_{{S}}$')
        if box_data:
            bp = ax.boxplot(box_data, patch_artist=True, medianprops=dict(color="grey", linewidth=2.5),
                            widths=0.7,
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
        return None, sys_df, None, None
    except Exception as e:
        print(f"Error parsing procfs file {procfs_file}: {e}")
        return None, None, None, None


def _environment_and_runtime_setup(env_key):
    """Map an environment directory name to its (environment label, runtime
    setup label) for the LaTeX table caption/column, e.g. ``RS2`` -> ``("Container", "RS2")``,
    ``Container``/``VM`` -> ``(env_key, "RS1")``."""
    if env_key and env_key.upper().startswith("RS") and env_key[2:].isdigit():
        return "Container", f"RS{env_key[2:]}"
    return env_key, "RS1"


def _per_run_cpu_util(run_paths, suffixes=('docker_tools', 'vm_tools')):
    """Per-run mean CPU utilization (%) from procfs, keyed by run label."""
    per_run = defaultdict(list)
    for run_path in run_paths:
        run_label = run_path.name
        for sub in run_path.iterdir():
            if sub.is_dir() and any(sub.name.endswith(suf) for suf in suffixes):
                # Use the JMeter steady-state window (trimmed 60s), matching the
                # convention of the power columns and of
                # createCpuUtilizationTableForAllLoadLevelsAndScenarios.py, so the
                # CPU_UTIL column agrees across both generated tables.
                jmeter_bounds = get_jmeter_time_bounds(str(sub), 60)
                experiment_log_path = sub / 'logs' / 'experiment_log.jsonl'
                service_pids = []
                if experiment_log_path.exists():
                    service_pids = extract_service_pids(str(experiment_log_path))
                files = list(sub.glob('**/procfs_*.csv'))
                for procfs_file in files:
                    try:
                        _, sys_df, _, _ = parse_procfs_data(str(procfs_file), service_pids, 80, 100, jmeter_bounds)
                        if sys_df is not None and not sys_df.empty:
                            sys_df = sys_df.copy()
                            sys_df['cpu_util'] = sys_df['delta_cpu'] / sys_df['interval'] / 80 * 100
                            per_run[run_label].extend(sys_df['cpu_util'].tolist())
                    except Exception as e:
                        print(f"Error parsing procfs for CPU util: {e}")
    return {run_label: float(np.mean(vals)) for run_label, vals in per_run.items() if vals}


def main():
    """
    Main entry point: collects data, generates and saves the boxplot PDF for all load levels.
    Handles results per environment (Container, VM, etc.) separately.
    """
    from shared import discover_environments

    trim_seconds = 60  # Number of seconds to trim at start and end of each time series
    # List of scenario suffixes to include and order
    scenario_suffixes = [
        "docker_tools",
    ]
    # Optional: custom labels for scenario suffixes
    custom_labels = {
        "idle_no_tools": "Idle",
        "docker_tools": "",
        "docker_none": "Container\nIdle (CI)",
        "docker_idle": "CI, Powercap (PC)\nand ProcFS",
        "docker_kepler": "CI, PC, ProcFS\nand Kepler",
        "docker_scaphandre": "CI, PC, ProcFS\nand Scaphandre",
        "docker_otjae": "CI, PC, ProcFS\nand OTJAE",
        "docker_joularjx": "CI, PC, ProcFS\nand JoularJX"
    }

    exp_results = Path(__file__).resolve().parent
    env_names = discover_environments(exp_results)
    if not env_names:
        env_names = [None]

    for env_name in env_names:
        env_key = env_name if env_name else 'flat'
        load_level_map = build_run_dirs(exp_results, environment=env_name) if env_name else build_run_dirs(exp_results)
        data_by_load = defaultdict(lambda: defaultdict(lambda: {'rittal': [], 'powercap': []}))

        label = f"[{env_name}] " if env_name else ""

        # --- Collect data per environment ---
        # Values are tagged with their run label (e.g. "690", "690_run2") so
        # the LaTeX table below can compute repetition-level statistics
        # (mean/std/CI) instead of pooling raw samples across repetitions.
        for load_level, dirs in load_level_map.items():
            for run_path in dirs:
                run_label = run_path.name
                for scenario_dir in run_path.iterdir():
                    if not scenario_dir.is_dir():
                        continue
                    scenario_name = scenario_dir.name
                    if scenario_suffixes is not None and not any(scenario_name.endswith(suf) for suf in scenario_suffixes):
                        continue
                    jmeter_bounds = get_jmeter_time_bounds(str(scenario_dir), trim_seconds)
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

        print(f"{label}Processed")

        # --- Plot per environment ---
        env_output = f"./power_consumption_boxplots_{env_key}_all_loads.pdf"
        plot_all_load_levels_in_one_row({env_key: data_by_load}, env_output, scenario_suffixes=scenario_suffixes, custom_labels=custom_labels)
        print(f"Environment {env_name}: Saved plot to {env_output}")

        # --- LaTeX Table per environment ---
        numeric_items = [(k, v) for k, v in data_by_load.items() if k.isdigit()]
        sorted_loads = sorted(numeric_items, key=lambda x: int(x[0]))

        # --- Delta Line Plot per environment ---
        load_labels_env = []
        pem_means_env = []
        ps_means_env = []
        # Session-paired P_EM - P_S differences across all load levels in this
        # environment (up to 4 load levels x 3 repetitions = n=12), used below
        # for a session-level effect size / significance summary.
        all_session_deltas = []
        for ll_key, scenario_dict in sorted_loads:
            pem_by_run = per_run_means(scenario_dict, 'tools', 'rittal')
            ps_by_run = per_run_means(scenario_dict, 'tools', 'powercap')
            common_runs = sorted(set(pem_by_run) & set(ps_by_run))
            all_session_deltas.extend(pem_by_run[r] - ps_by_run[r] for r in common_runs)
            Pem = float(np.mean(list(pem_by_run.values()))) if pem_by_run else None
            Ps = float(np.mean(list(ps_by_run.values()))) if ps_by_run else None
            if Pem is not None and Ps is not None:
                load_labels_env.append(int(ll_key) * 3)
                pem_means_env.append(Pem)
                ps_means_env.append(Ps)
        if pem_means_env and ps_means_env:
            pem0 = pem_means_env[0]
            ps0 = ps_means_env[0]
            pem_deltas = [v - pem0 for v in pem_means_env]
            ps_deltas = [v - ps0 for v in ps_means_env]
            plt.figure(figsize=(10,6))
            plt.plot(load_labels_env, pem_deltas, marker='o', label=r'$\Delta P_{EM}$')
            plt.plot(load_labels_env, ps_deltas, marker='s', label=r'$\Delta P_{S}$')
            plt.xlabel('Load Level (T/s)', fontsize=16)
            plt.ylabel('$\Delta P$ (W)', fontsize=16)
            plt.legend(fontsize=14)
            plt.grid(True, linestyle='--', alpha=0.7)
            plt.xticks(load_labels_env)
            plt.tight_layout()
            plt.savefig(f'./delta_power_vs_loadlevel_{env_key}.pdf', format='pdf', bbox_inches='tight')
            plt.close()
            print(f"Environment {env_name}: Saved delta power line plot to ./delta_power_vs_loadlevel_{env_key}.pdf")

        # --- LaTeX Table for this environment ---
        env_label, rs_label = _environment_and_runtime_setup(env_key)
        n_rows = len(sorted_loads)
        print(f"\n=== LaTeX Table ({env_key}) ===")
        print(f"% {env_key} Environment")
        print("\\begin{table}[!htbp]")
        print("    \\centering")
        print(f"    \\caption{{Mean system power consumption by load level (Environment: {env_label}, Runtime setup: {rs_label}, Test setup: TS1); cells show mean$\\pm$std across n=3 repetitions}}")
        print(f"    \\label{{tab:power_depending_on_utilization_{env_key.lower()}}}")
        print("        \\begin{tabular}{ |c|c|c|c|c| }")
        print("            \\hline")
        print("            Load (T/s)   & CPU\\textsubscript{UTIL} (\\%) & P\\textsubscript{EM} (W)& P\\textsubscript{S} (W)   & P\\textsubscript{$\\Delta$} (W)  \\\\")
        print("            \\hline")
        for row_idx, (load_level, scenario_dict) in enumerate(sorted_loads):
            pem_by_run = per_run_means(scenario_dict, 'tools', 'rittal')
            ps_by_run = per_run_means(scenario_dict, 'tools', 'powercap')
            common_runs = sorted(set(pem_by_run) & set(ps_by_run))
            pem_stats = summarize_repetitions(list(pem_by_run.values()))
            ps_stats = summarize_repetitions(list(ps_by_run.values()))
            delta_stats = summarize_repetitions([pem_by_run[r] - ps_by_run[r] for r in common_runs])

            cpu_util_by_run = _per_run_cpu_util(load_level_map.get(str(load_level), []))
            cpu_util_stats = summarize_repetitions(list(cpu_util_by_run.values()))

            load_label = f"{int(load_level)*3}"
            cpu_cell = fmt_mean_std(cpu_util_stats["mean"], cpu_util_stats["std"], unit="")
            pem_cell = fmt_mean_std(pem_stats["mean"], pem_stats["std"], unit="")
            ps_cell = fmt_mean_std(ps_stats["mean"], ps_stats["std"], unit="")
            delta_cell = fmt_mean_std(delta_stats["mean"], delta_stats["std"], unit="")
            print(f"            {load_label} & {cpu_cell} & {pem_cell} & {ps_cell} & {delta_cell} \\\\")
            if row_idx < n_rows - 1:
                print("            \\hhline{|-|-|-|-|-|}")
            else:
                print("            \\hline")
        print("        \\end{tabular}")
        print("\\end{table}")

        # --- Statistical note (session-paired P_EM vs P_S, n up to 12) ---
        d_paired = cohens_d_paired(all_session_deltas)
        wilcoxon = wilcoxon_signed_rank_exact(all_session_deltas)
        print(f"% Statistical note ({env_key}): P_EM vs P_S, session-paired across all load levels (n={wilcoxon['n_nonzero']}):")
        if d_paired is not None:
            print(f"%   Cohen's dz = {d_paired:.3f}")
        if wilcoxon["p_two_sided"] is not None:
            print(f"%   Wilcoxon signed-rank: W+={wilcoxon['statistic']:.1f}, p={wilcoxon['p_two_sided']:.4f} (p_floor={wilcoxon['p_floor']:.4f})")


# Run the script if executed directly
if __name__ == "__main__":
    main()
