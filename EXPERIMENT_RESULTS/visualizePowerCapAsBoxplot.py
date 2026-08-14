"""
visualizePowerCapAsBoxplot.py

Creates boxplots from raw powercap energy CSV files (energy in micro-Joules). This script converts energy deltas into power (Watts) and aggregates runs into boxplots by load and run type.
"""

import matplotlib.pyplot as plt
import seaborn as sns
import pandas as pd
import numpy as np
from pathlib import Path

# Input: powercap_*.csv files (cumulative energy in µJ) in the load-level
# subfolders of each environment.
from shared import (
    read_measurement_csv,
    build_run_dirs,
    summarize_repetitions,
    cohens_d_paired,
    wilcoxon_signed_rank_exact,
    fmt_mean_std,
)

base_dir = Path(__file__).resolve().parent
last_to_load = {
    "230": "690",
    "350": "1050",
    "480": "1440",
    "560": "1680",
}
run_mapping = {
    "spring_docker_otjae": "OTJAE",
    "spring_docker_joularjx": "JoularJX",
    "spring_docker_scaphandre": "Scaphandre",
    "spring_docker_kepler": "Kepler",
    "spring_docker_powerapi": "PowerAPI/SmartWatts",
    "spring_docker_tools": "none",
    "spring_vm_otjae": "OTJAE",
    "spring_vm_joularjx": "JoularJX",
    "spring_vm_scaphandre": "Scaphandre",
}

def find_powercap_files(environment=None):
    """Locate every ``powercap_*.csv`` belonging to a known test setup.

    Walks the run/scenario structure via ``build_run_dirs`` and keeps only
    scenario folders whose trailing tool suffix is one of the setups analysed
    here, mapping each to its display label (e.g. ``spring_docker_otjae`` ->
    ``OTJAE``). Load level 0 is intentionally not part of ``last_to_load`` and
    therefore excluded.

    Parameters
    ----------
    environment :
        Restrict to one environment (``"Container"``, ``"VM"``, …), or ``None``
        to scan all of them.

    Returns
    -------
    list of dict
        One entry per file with its path, load label in paper notation, raw
        numeric load, tool label, and repetition folder name.
    """
    rows = []

    # Use shared helper so we find directories both flat and nested (Container/VM/)
    if environment is not None:
        load_level_map = build_run_dirs(base_dir, environment=environment)
    else:
        load_level_map = build_run_dirs(base_dir)

    for last_dir, load_label in last_to_load.items():
        run_dirs = load_level_map.get(last_dir, [])
        if not run_dirs:
            continue
        for run_folder in run_dirs:
            if not run_folder.is_dir():
                continue
            # Numeric load extraction (e.g., 230, 230_run2, 230_run3 -> 230)
            numeric_load = last_dir
            for sub in run_folder.iterdir():
                if not sub.is_dir():
                    continue
                folder_name = sub.name
                tool_suffix = folder_name.split('_')[-1]
                allowed_suffixes = ["otjae", "joularjx", "scaphandre", "kepler", "tools", "idle", "none", "powerapi"]
                if tool_suffix not in allowed_suffixes:
                    continue
                run_name = run_mapping.get(folder_name)
                if run_name is None:
                    # Try with the suffix only (e.g., match "spring_vm_scaphandre" as "Scaphandre")
                    matching_keys = [k for k in run_mapping if k.endswith(tool_suffix)]
                    run_name = next((run_mapping[k] for k in matching_keys), None)
                if run_name is None:
                    continue
                for f in run_folder.glob(f"**/{folder_name}/powercap_*.csv"):
                    rows.append({"file": str(f), "load": load_label, "numeric_load": numeric_load, "run": run_name, "run_label": run_folder.name})
    return rows

def energy_to_power(df: pd.DataFrame) -> pd.DataFrame:
    """Convert cumulative RAPL energy counters into per-sample power in Watts.

    Expects the raw powercap reader columns ``Timestamp`` (ms),``Domain``,
    ``Energy (micro joules)``, and ``DRAM Energy (micro joules)``, where both
    energy columns are cumulative counters.

    Only ``package-*`` domains are considered, so each CPU socket contributes
    once and nested subdomains are not double-counted. Power is derived per
    domain as ``dE/dt`` and the domains are then summed per timestamp, giving
    the whole-system package power. Samples with a non-positive time delta or a
    negative energy delta are dropped, which discards RAPL counter overflows.
    A DRAM reading of ``-1`` means the domain is unavailable on this hardware
    and is treated as missing rather than as zero.

    Returns
    -------
    pandas.DataFrame
        Columns ``Timestamp``, ``P_pkg_W``, ``P_dram_W``, and
        ``Total Power (Watts)``. Empty (with the expected columns) if the input
        has no ``Domain`` column, which happens for malformed captures.
    """
    # Normalize the column names; the reader emits leading spaces on some of them
    df = df.rename(columns={
        " Energy (micro joules)": " Energy (micro joules)",
        " DRAM Energy (micro joules)": "DRAM Energy (micro joules)",
    })
    # Trim any remaining leading/trailing spaces
    df.columns = [c.strip() for c in df.columns]

    # Debug: print column names for first file to catch mismatches
    if 'Domain' not in df.columns:
        print(f"  WARNING: '{df.columns.tolist()}' missing 'Domain' column, skipping {df}")
        return pd.DataFrame(columns=["Timestamp", "Total Power (Watts)"])

    # Packages only (domain name starts with 'package-')
    df = df[df["Domain"].astype(str).str.startswith("package-")].copy()

    # Sort by domain and timestamp
    df["Timestamp"] = pd.to_numeric(df["Timestamp"], errors="coerce").astype("int64")
    df = df.sort_values(["Domain", "Timestamp"])

    # Compute the deltas per domain
    for col in ["Energy (micro joules)", "DRAM Energy (micro joules)"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    df["dE_pkg_uJ"] = df.groupby("Domain")["Energy (micro joules)"].diff()
    # DRAM may report -1 when unavailable -> ignore those readings
    if "DRAM Energy (micro joules)" in df.columns:
        dram_valid = df["DRAM Energy (micro joules)"].where(df["DRAM Energy (micro joules)"] >= 0, np.nan)
        df["dE_dram_uJ"] = df.groupby("Domain")[dram_valid.name].diff()
    else:
        df["dE_dram_uJ"] = np.nan

    df["dt_ms"] = df.groupby("Domain")["Timestamp"].diff()

    # Compute power: P = dE/dt; µJ/ms == mW, so divide by 1000 to get Watts
    df["P_pkg_W"] = (df["dE_pkg_uJ"] / df["dt_ms"]) / 1000.0
    df["P_dram_W"] = (df["dE_dram_uJ"] / df["dt_ms"]) / 1000.0

    # Keep only valid rows (positive dt, non-negative dE), which drops the
    # first sample per domain and any RAPL counter overflow
    df = df[(df["dt_ms"] > 0) & (df["dE_pkg_uJ"] >= 0)]

    # Aggregate across domains (sum over the CPU packages)
    agg = df.groupby("Timestamp").agg(
        P_pkg_W=("P_pkg_W", "sum"),
        P_dram_W=("P_dram_W", "sum"),
    ).reset_index()

    # Total power
    agg["Total Power (Watts)"] = agg["P_pkg_W"].fillna(0) + agg["P_dram_W"].fillna(0)

    return agg

def load_all_runs(file_rows):
    """Load and concatenate every file found by :func:`find_powercap_files`.

    Each file is converted to power and then trimmed to its steady-state
    window: samples from 60 s to 660 s relative to the start of the capture,
    which excludes the JVM warm-up at the beginning and the ramp-down at the
    end so only the fully loaded phase is compared across runs.

    Returns
    -------
    pandas.DataFrame
        All runs stacked, tagged with ``load``, ``run`` (tool label), and
        ``run_label`` (repetition folder). Empty with the expected columns when
        *file_rows* is empty.
    """
    data_frames = []
    for info in file_rows:
        df_raw = read_measurement_csv(info["file"])
        df_pow = energy_to_power(df_raw)
        t0 = df_pow["Timestamp"].min()
        df_pow["Relative Timestamp"] = df_pow["Timestamp"] - t0
        df_pow = df_pow[(df_pow["Relative Timestamp"] >= 60000) & (df_pow["Relative Timestamp"] <= 660000)].copy()
        # Use numeric_load for aggregation
        df_pow["load"] = info["numeric_load"]
        df_pow["run"] = info["run"]
        df_pow["run_label"] = info["run_label"]
        data_frames.append(df_pow)
    if not data_frames:
        return pd.DataFrame(columns=["Timestamp", "Total Power (Watts)", "load", "run", "run_label"])
    return pd.concat(data_frames, ignore_index=True)

def main():
    """Generate the total-power boxplots and print the summary table.

    Produces one ``../boxplot_total_power_by_load_and_run_{env}.pdf`` per
    environment — the "power distribution of experiment runs" figure of the
    paper — plus a combined summary table across environments.

    Note the ``../`` in the output path: this script writes one level above the
    current working directory and therefore has to be started from inside
    ``EXPERIMENT_RESULTS/``, not from the repository root.
    """
    from shared import discover_environments

    exp_results = Path(__file__).resolve().parent
    env_names = discover_environments(exp_results)
    if not env_names:
        env_names = [None]

    all_powercap_envs = {}

    for env_name in env_names:
        csv_files = find_powercap_files(environment=env_name)
        if not csv_files:
            print(f"[{env_name}] No powercap_*.csv files found.")
            continue

        label = f"[{env_name}] " if env_name else ""
        print(f"{label}Processing {len(csv_files)} files...")

        df_all = load_all_runs(csv_files)

        load_order = ["230", "350", "480", "560"]
        run_order = ["none", "OTJAE", "JoularJX", "Scaphandre", "Kepler", "PowerAPI/SmartWatts"]
        palette = sns.color_palette("Set2", n_colors=len(run_order))

        fig = plt.figure(figsize=(14, 10))
        ax = sns.boxplot(
            x="load",
            y="Total Power (Watts)",
            hue="run",
            data=df_all,
            order=load_order,
            hue_order=run_order,
            palette=palette,
            dodge=True
        )
        ax.yaxis.grid(True)

        handles, labels = ax.get_legend_handles_labels()
        ax.legend(handles=handles, labels=labels, loc="upper left", title=None, fontsize=14)

        new_labels = [str(int(l)*3) for l in load_order]
        ax.set_xticklabels(new_labels, fontsize=14)

        plt.xlabel("Load (T/s)", fontsize=14)
        plt.ylabel("$P_{S}$ (W)", fontsize=14)

        env_key = env_name if env_name else 'flat'
        pdf_path = f"../boxplot_total_power_by_load_and_run_{env_key}.pdf"
        plt.tight_layout()
        plt.savefig(pdf_path, format="pdf", bbox_inches="tight", pad_inches=0)
        plt.close()
        print(f"{label}Box plot successfully saved as PDF: {pdf_path}")

        all_powercap_envs[env_key] = df_all

    # Print combined summary table: collapse each repetition's raw per-second
    # samples to one scalar per (run type, load, run_label) *before* any
    # cross-repetition averaging, matching the mean-of-per-run-means
    # convention used throughout the paper.
    if all_powercap_envs:
        print("\n=== Summary by Environment ===")
        for env_key, df in all_powercap_envs.items():
            print(f"\n--- Environment: {env_key} ---")
            per_run_means = (
                df.groupby(["run", "load", "run_label"])["Total Power (Watts)"]
                .mean()
                .reset_index()
            )
            for load in ["230", "350", "480", "560"]:
                for run_name in run_order:
                    vals = per_run_means[(per_run_means["load"] == load) & (per_run_means["run"] == run_name)]["Total Power (Watts)"].tolist()
                    if not vals:
                        continue
                    stats = summarize_repetitions(vals)
                    print(f"  Load {load} ({int(load)*3} T/s), {run_name}: {fmt_mean_std(stats['mean'], stats['std'])} (n={stats['n']})")

            # --- Statistical notes: each tool's total power vs the       ---
            # --- "none" baseline. Total system power scales strongly     ---
            # --- with load (the same confound identified for the CPU     ---
            # --- utilization overhead table), so pooling raw values      ---
            # --- across load levels as independent samples would be      ---
            # --- swamped by load-driven variance. Instead, each          ---
            # --- repetition's deviation from "none"'s own mean at that   ---
            # --- load level is computed first, and only these deviations ---
            # --- are pooled across load levels (one-sample Wilcoxon test ---
            # --- on load-centered deviations, mirroring the CPU          ---
            # --- utilization table's fix).                               ---
            print(f"\n  --- Statistical notes ({env_key}): tool vs baseline ('none'), load-centered deviations pooled across load levels ---")
            deviations_by_tool = {run_name: [] for run_name in run_order if run_name != "none"}
            for load in ["230", "350", "480", "560"]:
                baseline_vals_this_level = per_run_means[(per_run_means["load"] == load) & (per_run_means["run"] == "none")]["Total Power (Watts)"].tolist()
                if not baseline_vals_this_level:
                    continue
                baseline_mean_this_level = float(np.mean(baseline_vals_this_level))
                for run_name in deviations_by_tool:
                    tool_vals_this_level = per_run_means[(per_run_means["load"] == load) & (per_run_means["run"] == run_name)]["Total Power (Watts)"].tolist()
                    deviations_by_tool[run_name].extend(v - baseline_mean_this_level for v in tool_vals_this_level)
            for run_name, deviations in deviations_by_tool.items():
                if not deviations:
                    continue
                d_paired = cohens_d_paired(deviations)
                wilcoxon = wilcoxon_signed_rank_exact(deviations)
                print(f"  % Statistical note ({env_key}, {run_name} vs none), one-sample Wilcoxon test on deviations from the same-load-level baseline mean (n={wilcoxon['n_nonzero']}):")
                if d_paired is not None:
                    print(f"  %   Cohen's dz = {d_paired:.3f}")
                if wilcoxon["p_two_sided"] is not None:
                    print(f"  %   Wilcoxon signed-rank: W+={wilcoxon['statistic']:.1f}, p={wilcoxon['p_two_sided']:.4f} (p_floor={wilcoxon['p_floor']:.4f})")

if __name__ == "__main__":
    main()