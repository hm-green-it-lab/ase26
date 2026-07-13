"""
createCpuUtilizationTableForAllLoadLevelsAndScenarios.py

Scan experiment result folders and compute mean CPU utilization per load and scenario.
The script generates LaTeX table output (printed to stdout) suitable for inclusion in a report.
Tables are generated per environment (Container, VM, ...).
"""

import pandas as pd
import numpy as np

from pathlib import Path
from collections import defaultdict
import json
import argparse

from shared import build_run_dirs, get_jmeter_time_bounds, extract_service_pids


def parse_procfs_data(procfs_file, service_pids, n_cores=80, ticks_per_sec=100, jmeter_bounds=None):
    """
    Stub: returns only sys_df (system CPU utilization).
    The full implementation that also returns proc_util/mem/io deltas lives in shared.py
    and in the per‑process scripts.  This script only needs sys_df.
    """
    try:
        df = pd.read_csv(procfs_file)
        min_time = pd.to_datetime(df["Timestamp"].min(), unit="ms")
        max_time = pd.to_datetime(df["Timestamp"].max(), unit="ms")
        trim_seconds = 60
        if jmeter_bounds is not None and all(jmeter_bounds):
            start, end = jmeter_bounds
        else:
            start = min_time + pd.Timedelta(seconds=trim_seconds)
            end = max_time - pd.Timedelta(seconds=trim_seconds)
        df = df[(pd.to_datetime(df["Timestamp"], unit="ms") >= start) & (pd.to_datetime(df["Timestamp"], unit="ms") <= end)]
        stat_mask = df["SourceFile"].str.endswith(r"stat")
        stat_df = df[stat_mask].copy()
        stat_df["datetime"] = pd.to_datetime(stat_df["Timestamp"], unit="ms")
        stat_df["userTime_s"] = stat_df["userTime (Ticks)"] / ticks_per_sec
        stat_df["systemTime_s"] = stat_df["systemTime (Ticks)"] / ticks_per_sec
        sys_df = stat_df[stat_df["SourceFile"] == "/proc/stat"].sort_values("datetime")
        sys_df = sys_df[["datetime", "userTime_s", "systemTime_s"]].copy()
        sys_df["total_cpu"] = sys_df["userTime_s"] + sys_df["systemTime_s"]
        sys_df["delta_cpu"] = sys_df["total_cpu"].diff()
        sys_df["interval"] = sys_df["datetime"].diff().dt.total_seconds()
        sys_df = sys_df.iloc[1:]
        return None, sys_df, None, None
    except Exception as e:
        print(f"Error parsing procfs file {procfs_file}: {e}")
        return None, None, None, None


def generate_table_for_env(exp_results, env_name):
    """
    Generate LaTeX table for a single environment.
    Returns the LaTeX string, or None if no data found.
    """
    trim_seconds = 60
    scenario_suffixes = [
        "docker_tools",
        "docker_kepler",
        "docker_scaphandre",
        "docker_powerapi",
        "docker_otjae",
        "docker_joularjx",
        "spring_vm_scaphandre",
        "spring_vm_joularjx",
        "spring_vm_otjae",
    ]

    load_level_map = build_run_dirs(exp_results, environment=env_name)
    numeric_loads = sorted([k for k in load_level_map.keys() if k.isdigit()], key=int)

    is_vm_env = env_name.startswith("VM")

    if env_name.startswith("VM"):
        scenario_columns = [
            ("spring_vm_scaphandre", "CPU\\textsubscript{Sc}"),
            ("spring_vm_otjae", "CPU\\textsubscript{O}"),
            ("spring_vm_joularjx", "CPU\\textsubscript{J}")
        ]
    else:
        scenario_columns = [
            ("docker_tools", "CPU\\textsubscript{none}"),
            ("docker_kepler", "CPU\\textsubscript{K}"),
            ("docker_scaphandre", "CPU\\textsubscript{Sc}"),
            ("docker_powerapi", "CPU\\textsubscript{PA}"),
            ("docker_otjae", "CPU\\textsubscript{O}"),
            ("docker_joularjx", "CPU\\textsubscript{J}")
        ]

    n_cols = len(scenario_columns) + 1
    tab_col_spec = "|c|" * n_cols + "|"

    lines = []
    host_lines = []
    vm_lines = []
    lines.append("\\begin{table*}")
    lines.append("    \\begin{center}")
    lines.append(r"        \caption{Mean CPU utilization of measurement runs in environment: " + env_name + "}")
    lines.append(f"        \\begin{{tabular}}{{{tab_col_spec.strip()}}}")
    lines.append("            \\hline")
    lines.append("            Load & " + " & ".join([col for _, col in scenario_columns]) + "\\\\")
    lines.append("            \\hline")

    for load_level in numeric_loads:
    
        if is_vm_env:
            host_row = []
            vm_row = []
        else:
            row = []
        for suffix, _ in scenario_columns:
            if is_vm_env:
                host_cpu_utils = []
                vm_cpu_utils = []
            else:
                cpu_utils = []
            run_paths = load_level_map.get(str(load_level), [])
            for run_path in run_paths:
                for sub in run_path.iterdir():
                    if sub.is_dir() and sub.name.endswith(suffix):
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
                                    sys_df["cpu_util"] = sys_df["delta_cpu"] / sys_df["interval"] / 80 * 100
                                    mean_cpu = sys_df["cpu_util"].mean()
                                    if is_vm_env:
                                        if procfs_file.name.count("_vm_") >= 2:
                                            vm_cpu_utils.append(mean_cpu)
                                        else:
                                            host_cpu_utils.append(mean_cpu)
                                    else:
                                        cpu_utils.append(mean_cpu)
                            except Exception as e:
                                print(f"Error parsing procfs for CPU util: {e}")
            if is_vm_env:

                host_row.append(
                f"{np.mean(host_cpu_utils):.2f}\\%"
                    if host_cpu_utils else "-"
                )

                vm_row.append(
                f"{np.mean(vm_cpu_utils):.2f}\\%"
                if vm_cpu_utils else "-"
                )

            else:

                row.append(
                    f"{np.mean(cpu_utils):.2f}\\%"
                    if cpu_utils else "-"
                )
        load_label = f"{int(load_level)*3}T/s"
        if is_vm_env:
            host_lines.append(
                f"            {load_label} & " + " & ".join(host_row) + " \\\\"
            )
            host_lines.append("            \\hline")
            vm_lines.append(
                f"            {load_label} & " + " & ".join(vm_row) + " \\\\"
            )
            vm_lines.append("            \\hline")
        else:
            lines.append(
                f"            {load_label} & " + " & ".join(row) + " \\\\"
            )
            lines.append("            \\hline")

    if not numeric_loads:
        lines.append("        \\end{tabular}")
        lines.append("        \\end{center}")
        lines.append("\\end{table*}")
        return "\n".join(lines)
    
    if not is_vm_env:
        lines.append("        \\end{tabular}")
        lines.append("    \\end{center}")
        lines.append("\\end{table*}")
        return "\n".join(lines)
    
    host_table = [
        "\\begin{table*}",
        "    \\begin{center}",
        rf"        \caption{{Mean CPU utilization (Host/SUT) in environment: {env_name}}}",
        f"        \\begin{{tabular}}{{{{{tab_col_spec.strip()}}}}}",
    ]

    host_table.extend(host_lines)

    host_table.extend([
        "        \\end{tabular}",
        "    \\end{center}",
        "\\end{table*}",
    ])
    vm_table = [
        "\\begin{table*}",
        "    \\begin{center}",
        rf"        \caption{{Mean CPU utilization (Guest VM) in environment: {env_name}}}",
        f"        \\begin{{tabular}}{{{{{tab_col_spec.strip()}}}}}",
        "            \\hline",
        "            Load & "
        + " & ".join([col for _, col in scenario_columns])
        + "\\\\",
        "            \\hline",
    ]

    vm_table.extend(vm_lines)

    vm_table.extend([
        "        \\end{tabular}",
        "    \\end{center}",
        "\\end{table*}",
    ])
    return (
        "\n".join(host_table)
        + "\n\n"
        + "\n".join(vm_table)
    )

def main():
    """
    Main entry point: discovers environments and generates a separate LaTeX table for each.
    """
    exp_results = Path(__file__).resolve().parent

    from shared import discover_environments

    environments = discover_environments(exp_results)
    if not environments:
        environments = ['']

    for env in environments:
        print()
        print("=" * 60)
        env_label = env.upper() if env else "All"
        print(f"  Environment: {env_label}")
        print("=" * 60)
        latex = generate_table_for_env(exp_results, env)
        print(latex)

    if not environments:
        print()
        print("=" * 60)
        print("  Environment: (none - flat structure)")
        print("=" * 60)
        latex = generate_table_for_env(exp_results, "")
        print(latex)


if __name__ == "__main__":
    main()
