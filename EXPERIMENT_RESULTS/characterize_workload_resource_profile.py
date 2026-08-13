"""
characterize_workload_resource_profile.py

Quantify the test application's per-request resource-use profile (CPU time,
memory allocation, disk I/O, network I/O) per HTTP method (GET/POST/DELETE),
using OTJAE's per-transaction resource-demand instrumentation.

This addresses the reviewer request to quantify workload resource intensity
(CPU/network/disk) rather than only describing it qualitatively: the array
sizes (3000/4000/6000 for GET/POST/DELETE) determine resource demand, but the
actual measured demand was never reported.

Data source: OTJAE's docker_compose_logs_*.txt files from the RS1 Container
environment (test setup TS6, all load levels/repetitions), parsed with
parse_otjae_transaction_resource() (reused from
visualizeLoadLevelTransactionPowerConsumptionAsBoxplots.py) to get, per run,
per-second, per-method sums of CPU time (ns), heap allocation (bytes), disk
I/O (bytes), and network I/O (bytes), plus the request count.

Per run (one load level + one repetition), each sum is divided by that run's
request count for that method to get a per-invocation mean -- one scalar per
run. Values are pooled across all load levels and repetitions (matching the
"RS1 Container, all load levels" scope) and summarized with
summarize_repetitions(), since the per-request resource cost is a property of
the fixed-size workload, not of the offered load, and pooling shows whether
that holds (stable mean, small std) or degrades under contention (large std).
"""

from pathlib import Path
from collections import defaultdict

from shared import (
    build_run_dirs,
    get_jmeter_time_bounds,
    scenario_matches,
    summarize_repetitions,
    fmt_mean_std,
)
from visualizeLoadLevelTransactionPowerConsumptionAsBoxplots import (
    parse_otjae_transaction_resource,
)

TRIM_SECONDS = 60
METHODS = ("GET", "POST", "DELETE")


def per_run_invocation_means(log_file, jmeter_bounds) -> dict[str, dict[str, float]]:
    """Aggregate one run's per-second OTJAE resource deltas into one
    per-invocation mean per method: {method: {"cpu_ms", "mem_kb", "disk_kb",
    "net_kb", "count"}}. Returns an empty dict for methods with no requests.
    """
    per_second = parse_otjae_transaction_resource(str(log_file), jmeter_bounds)
    totals = defaultdict(lambda: {"cpu": 0, "mem": 0, "disk": 0, "net": 0, "count": 0})
    for _sec, methods in per_second.items():
        for method, vals in methods.items():
            if method not in METHODS:
                continue
            t = totals[method]
            t["cpu"] += vals["cpu"]
            t["mem"] += vals["mem"]
            t["disk"] += vals["disk"]
            t["net"] += vals["net"]
            t["count"] += vals["count"]

    result = {}
    for method, t in totals.items():
        if t["count"] <= 0:
            continue
        result[method] = {
            "cpu_ms": (t["cpu"] / t["count"]) / 1e6,     # ns -> ms
            "mem_kb": (t["mem"] / t["count"]) / 1024,    # bytes -> KB
            "disk_kb": (t["disk"] / t["count"]) / 1024,  # bytes -> KB
            "net_kb": (t["net"] / t["count"]) / 1024,    # bytes -> KB
            "count": t["count"],
        }
    return result


def collect_samples() -> dict[str, dict[str, list[float]]]:
    """Return {method: {"cpu_ms": [...], "mem_kb": [...], "disk_kb": [...],
    "net_kb": [...]}}, one value per (load level, repetition) run, pooled
    across all RS1 Container load levels/repetitions, plus a total request
    count per method for transparency.
    """
    exp_results = Path(__file__).resolve().parent
    run_dirs = build_run_dirs(exp_results, environment="Container")

    samples = {m: {"cpu_ms": [], "mem_kb": [], "disk_kb": [], "net_kb": []} for m in METHODS}
    total_requests = {m: 0 for m in METHODS}

    for load_level, dirs in run_dirs.items():
        if not str(load_level).isdigit() or int(load_level) == 0:
            continue
        for run_path in dirs:
            for scenario_dir in run_path.iterdir():
                if not scenario_dir.is_dir() or not scenario_matches(scenario_dir.name, "otjae"):
                    continue
                log_files = list(scenario_dir.glob("**/docker_compose_logs_*.txt"))
                if not log_files:
                    continue
                jmeter_bounds = get_jmeter_time_bounds(str(scenario_dir), TRIM_SECONDS)
                run_means = per_run_invocation_means(log_files[0], jmeter_bounds)
                for method, vals in run_means.items():
                    samples[method]["cpu_ms"].append(vals["cpu_ms"])
                    samples[method]["mem_kb"].append(vals["mem_kb"])
                    samples[method]["disk_kb"].append(vals["disk_kb"])
                    samples[method]["net_kb"].append(vals["net_kb"])
                    total_requests[method] += vals["count"]

    return samples, total_requests


def main():
    samples, total_requests = collect_samples()

    print("=== Per-request resource-use profile (RS1 Container, all load levels/repetitions) ===\n")
    for method in METHODS:
        s = samples[method]
        n_runs = len(s["cpu_ms"])
        print(f"{method} (n={n_runs} load-level/repetition samples, {total_requests[method]} requests total):")
        for metric, unit in (("cpu_ms", "ms"), ("mem_kb", "KB"), ("disk_kb", "KB"), ("net_kb", "KB")):
            stats = summarize_repetitions(s[metric])
            print(f"  {metric}: {fmt_mean_std(stats['mean'], stats['std'], unit=unit, decimals=2)} (n={stats['n']})")
        print()

    print("=== LaTeX table ===\n")
    print(r"""\begin{table}[h]
    \begin{center}
        \begin{tabular}{ |c|c|c|c|c| }
            \hline
            HTTP method & CPU time & Memory & Disk I/O & Network I/O \\
            \hline""")
    for method in METHODS:
        s = samples[method]
        cpu = summarize_repetitions(s["cpu_ms"])
        mem = summarize_repetitions(s["mem_kb"])
        disk = summarize_repetitions(s["disk_kb"])
        net = summarize_repetitions(s["net_kb"])
        print(
            f"            {method} & "
            f"{fmt_mean_std(cpu['mean'], cpu['std'], unit='ms', decimals=2)} & "
            f"{fmt_mean_std(mem['mean'], mem['std'], unit='KB', decimals=1)} & "
            f"{fmt_mean_std(disk['mean'], disk['std'], unit='KB', decimals=1)} & "
            f"{fmt_mean_std(net['mean'], net['std'], unit='KB', decimals=1)} \\\\"
        )
        print("            \\hline")
    print(r"""        \end{tabular}
        \caption{Per-request resource-use profile of the test application by HTTP method, measured via OTJAE's resource-demand instrumentation (RS1 Container, pooled across all load levels and repetitions; mean$\pm$std)}
        \label{tab:workload_resource_profile}
    \end{center}
\end{table}""")


if __name__ == "__main__":
    main()
