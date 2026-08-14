"""
createResponseTimeTableForAllLoadLevelsAndScenarios.py

Scan experiment result folders and compute mean JMeter response time (``elapsed``,
in ms) per load level, HTTP method (GET/POST/DELETE), and scenario, restricted to
each run's own steady-state window (first/last 60s discarded, matching the
12-minute-run / 10-minute-steady-state convention used elsewhere). The script
generates LaTeX table output (printed to stdout) suitable for inclusion in a
report, structured like createCpuUtilizationTableForAllLoadLevelsAndScenarios.py
but with an additional per-HTTP-method row grouping. Tables are generated per
environment (Container, VM, ...).
"""

import numpy as np
import pandas as pd

from pathlib import Path
from collections import defaultdict

from shared import (
    build_run_dirs, discover_environments,
    summarize_repetitions, fmt_mean_std, cohens_d_one_sample, wilcoxon_signed_rank_exact,
)

METHODS = ["GET", "POST", "DELETE"]


def _normalize_success(value) -> str:
    """Normalize a JMeter .jtl 'success' field value for robust true/false matching."""
    if value is None:
        return ""
    return str(value).strip().strip('"').strip("'").lower()


def parse_jtl_response_times(jtl_file, trim_seconds: float = 60) -> dict:
    """
    Parse one JMeter .jtl file and return {method: [elapsed_ms, ...]} for
    successful requests, restricted to the file's own steady-state window
    (the first/last trim_seconds discarded based on its own timeStamp range).
    """
    try:
        df = pd.read_csv(jtl_file, low_memory=False)
    except Exception as e:
        print(f"Error reading {jtl_file}: {e}")
        return {}
    if df.empty or not {"timeStamp", "elapsed", "label"}.issubset(df.columns):
        return {}

    if trim_seconds > 0:
        min_ts = df["timeStamp"].min()
        max_ts = df["timeStamp"].max()
        start = min_ts + trim_seconds * 1000
        end = max_ts - trim_seconds * 1000
        df = df[(df["timeStamp"] >= start) & (df["timeStamp"] <= end)]

    if "success" in df.columns:
        df = df[df["success"].apply(_normalize_success) == "true"]

    method = df["label"].astype(str).str.split(" ").str[0].str.upper()

    result = {}
    for m in METHODS:
        values = df.loc[method == m, "elapsed"]
        if not values.empty:
            result[m] = values.astype(float).tolist()
    return result


def generate_table_for_env(exp_results, env_name):
    """
    Generate one LaTeX table (rows: load level x HTTP method, columns: scenario)
    for a single environment. Returns the LaTeX string, or None if no data found.
    """
    trim_seconds = 60

    is_vm_env = env_name.startswith("VM")

    if is_vm_env:
        scenario_columns = [
            ("spring_vm_tools", "None"),
            ("spring_vm_scaphandre", "Scaphandre"),
            ("spring_vm_otjae", "OTJAE"),
            ("spring_vm_joularjx", "JoularJX"),
        ]
    else:
        scenario_columns = [
            ("docker_tools", "None"),
            ("docker_kepler", "Kepler"),
            ("docker_scaphandre", "Scaphandre"),
            ("docker_powerapi", "PowerAPI"),
            ("docker_otjae", "OTJAE"),
            ("docker_joularjx", "JoularJX"),
        ]

    load_level_map = build_run_dirs(exp_results, environment=env_name)
    numeric_loads = sorted([k for k in load_level_map.keys() if k.isdigit()], key=int)

    tab_col_spec = "|c|c|" + "c|" * len(scenario_columns)

    lines = []
    lines.append("\\begin{table*}")
    lines.append("    \\begin{center}")
    lines.append(rf"        \caption{{Mean response time (ms) of measurement runs in environment: {env_name}}}")
    lines.append("        \\resizebox{\\textwidth}{!}{%")
    lines.append(f"        \\begin{{tabular}}{{{tab_col_spec}}}")
    lines.append("            \\hline")
    header = "            Load & Method & " + " & ".join(f"{label} (ms)" for _, label in scenario_columns) + " \\\\"
    lines.append(header)
    lines.append("            \\hline")

    # Per-run deviation from the None baseline's own mean *at that load level
    # and method*, pooled across all load levels, per (suffix, method) --
    # mirrors the same load-driven-variance-isolation approach used in
    # createCpuUtilizationTableForAllLoadLevelsAndScenarios.py.
    pooled_deviation_by_suffix_method = defaultdict(list)

    for load_level in numeric_loads:
        # The raw load-level folder name already denotes the per-HTTP-method
        # target throughput (the JMeter script distributes an equal share of
        # requests to each of the three methods); the "*3" convention used
        # elsewhere in this codebase is the *total* system throughput summed
        # across all three methods, which would misrepresent a single
        # method's own row here.
        load_label = f"{int(load_level)}T/s"

        # First pass: collect per-run (per repetition) mean response time per
        # (scenario, method), so the table can be emitted method-major
        # without re-scanning the filesystem for each method.
        per_scenario_method_runs = {}
        for suffix, _ in scenario_columns:
            method_runs = defaultdict(list)
            run_paths = load_level_map.get(str(load_level), [])
            for run_path in run_paths:
                for sub in run_path.iterdir():
                    if not (sub.is_dir() and sub.name.endswith(suffix)):
                        continue
                    run_method_values = defaultdict(list)
                    for jtl_file in sub.glob("**/*.jtl"):
                        for method, values in parse_jtl_response_times(str(jtl_file), trim_seconds).items():
                            run_method_values[method].extend(values)
                    for method, values in run_method_values.items():
                        if values:
                            method_runs[method].append(float(np.mean(values)))
            per_scenario_method_runs[suffix] = method_runs

        for method in METHODS:
            row = []
            baseline_mean_this_level = None
            for suffix, _ in scenario_columns:
                runs = per_scenario_method_runs.get(suffix, {}).get(method, [])
                stats = summarize_repetitions(runs)
                row.append(fmt_mean_std(stats["mean"], stats["std"], unit=""))
                if suffix in ("docker_tools", "spring_vm_tools"):
                    baseline_mean_this_level = stats["mean"]
                elif baseline_mean_this_level is not None:
                    pooled_deviation_by_suffix_method[(suffix, method)].extend(
                        v - baseline_mean_this_level for v in runs
                    )
            lines.append(f"            {load_label} & {method} & " + " & ".join(row) + " \\\\")
        lines.append("            \\hline")

    lines.append("        \\end{tabular}")
    lines.append("        }")
    lines.append("    \\end{center}")
    lines.append("\\end{table*}")

    # --- Statistical notes: each tool's response-time overhead vs the ---
    # --- None baseline, pooled across all load levels as the deviation ---
    # --- from that load level's own baseline mean, per HTTP method. ---
    for suffix, col_label in scenario_columns:
        if suffix in ("docker_tools", "spring_vm_tools"):
            continue
        for method in METHODS:
            deviations = pooled_deviation_by_suffix_method.get((suffix, method), [])
            if not deviations:
                continue
            d_one_sample = cohens_d_one_sample(deviations)
            wilcoxon = wilcoxon_signed_rank_exact(deviations)
            lines.append(
                f"% Statistical note ({env_name}, {col_label} {method} vs None), "
                f"one-sample Wilcoxon test on deviations from the same-load-level baseline mean (n={wilcoxon['n_nonzero']}):"
            )
            if d_one_sample is not None:
                lines.append(f"%   Cohen's d = {d_one_sample:.3f}")
            if wilcoxon["p_two_sided"] is not None:
                lines.append(f"%   Wilcoxon signed-rank: W+={wilcoxon['statistic']:.1f}, p={wilcoxon['p_two_sided']:.4f} (p_floor={wilcoxon['p_floor']:.4f})")

    return "\n".join(lines)


def main():
    """
    Main entry point: discovers environments and generates a separate LaTeX table for each.
    """
    exp_results = Path(__file__).resolve().parent
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


if __name__ == "__main__":
    main()
