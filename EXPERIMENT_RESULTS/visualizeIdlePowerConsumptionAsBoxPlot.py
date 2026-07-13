"""
visualizeIdlePowerConsumptionAsBoxPlot.py

Create boxplots for idle power consumption across different experimental scenarios. The script supports EM (Rittal) and RAPL (powercap) inputs and produces a two-panel figure comparing the two measurement types.
"""

import pandas as pd
import numpy as np
import matplotlib.pyplot as plt
from pathlib import Path

from shared import (
    load_power_data,
    discover_environments,
    build_run_dirs,
    trim_time_series as _shared_trim_time_series,
)


# ---------------------------------------------------------------------------
# visualizeIdlePowerConsumptionAsBoxPlot.py
#
# This script is the ONLY consumer of ``calculate_power_from_energy`` that
# requires the *verbose* negative-power warning.  The shared version prints
# a single line; this wrapper prints a full diagnostic block.
# ---------------------------------------------------------------------------

def calculate_power_from_energy(df, trim_seconds=0):
    """Wrapper around shared.calculate_power_from_energy with verbose diagnostics."""
    from shared import calculate_power_from_energy as _shared_calc

    df = df.sort_values(["Timestamp", "Domain"])
    df["datetime"] = pd.to_datetime(df["Timestamp"], unit="ms")
    power_data = []

    for domain in df["Domain"].unique():
        domain_data = df[df["Domain"] == domain].copy()
        energy_diff = domain_data[" Energy (micro joules)"].diff()
        time_diff = domain_data["datetime"].diff().dt.total_seconds()
        power = energy_diff / (time_diff * 1_000_000)

        if (power < 0).any():
            negative_indices = power[power < 0].index
            for idx in negative_indices:
                print(f"\nNegative power value found for Domain {domain}:")
                print(f"Timestamp: {domain_data.loc[idx, 'datetime']} {domain_data.loc[idx, 'Timestamp']}")
                print(f"Energy diff: {energy_diff[idx]} \u00b5J")
                print(f"Time diff: {time_diff[idx]} s")
                print(f"Calculated power: {power[idx]} W")
                print("\nInvolved records:")
                print(f"Current record:")
                print(domain_data.loc[idx, [" Energy (micro joules)", "datetime"]].to_string())
                print(f"Skipping negative power value for Domain {domain} as RAPL might have overflown")
                power[idx] = np.nan

        domain_data["Power"] = power
        domain_data = domain_data.dropna(subset=["Power"])
        power_data.append(domain_data)

    result = pd.concat(power_data)
    domain_counts = result.groupby("datetime")["Domain"].count()
    complete_timestamps = domain_counts[domain_counts == len(df["Domain"].unique())].index
    result_filtered = result[result["datetime"].isin(complete_timestamps)]
    power_sum = result_filtered.groupby("datetime")["Power"].sum().reset_index()

    if trim_seconds > 0:
        power_sum = _shared_trim_time_series(power_sum, trim_seconds)

    return power_sum


def load_power_data_v2(file_path, trim_seconds=0):
    """Custom wrapper that routes through our verbose calculate_power_from_energy."""
    if "rittal" in file_path.lower():
        from shared import load_rittal_data
        return load_rittal_data(file_path, trim_seconds)
    elif "powercap" in file_path.lower():
        df = pd.read_csv(file_path)
        return calculate_power_from_energy(df, trim_seconds)
    else:
        raise ValueError(f"Unknown file type: {file_path}")


def create_power_consumption_boxplot(data_dirs, output_path, custom_labels=None, trim_seconds=0):
    """
    Creates a boxplot of power consumption for different experiments with separate y-axes
    for RAPL and EM measurements
    Optionally, you can specify 'scenario_order' (list of suffixes) to control the order of boxplots.
    """
    scenario_order = [
        "idle_no_tools",
        "docker_none",
        "docker_idle",
        "docker_scaphandre",
        "docker_kepler",
        "docker_powerapi",
        "docker_joularjx",
        "docker_otjae"]  # Set to a list of suffixes to control order, or pass as argument
    # --- New logic: group by scenario suffix, aggregate all runs ---
    from collections import defaultdict
    scenario_rittal = defaultdict(list)
    scenario_powercap = defaultdict(list)
    scenario_labels = {}

    for dir_path in data_dirs:
        dir_path = Path(dir_path)
        # Extract scenario key: everything after the timestamp (4th underscore)
        # Example: 20250923_174052_baseline_idle_no_tools -> idle_no_tools
        parts = dir_path.name.split('_', 3)
        if len(parts) < 4:
            print(f"Directory name format unexpected: {dir_path.name}")
            continue
        scenario_key = parts[3]
        # Use custom label if available, else scenario_key
        label = custom_labels.get(scenario_key) if custom_labels else scenario_key
        scenario_labels[scenario_key] = label

        power_files = list(dir_path.glob('**/rittal_*.csv')) + list(dir_path.glob('**/powercap_*.csv'))
        if not power_files:
            print(f"No power data found in: {dir_path}")
            continue
        for file_path in power_files:
            print(f"Using data file: {file_path}")
            try:
                power_data = load_power_data_v2(str(file_path), trim_seconds)
                if 'Power' in power_data.columns:
                    power_values = power_data['Power']
                else:
                    power_values = power_data['Power (Watts)']
                if 'rittal' in file_path.name.lower():
                    scenario_rittal[scenario_key].append(power_values)
                else:
                    scenario_powercap[scenario_key].append(power_values)
            except Exception as e:
                print(f"Error loading {file_path}: {e}")


    # Determine the order of scenarios
    if scenario_order is not None:
        ordered_rittal = [(k, scenario_rittal[k]) for k in scenario_order if k in scenario_rittal]
        ordered_powercap = [(k, scenario_powercap[k]) for k in scenario_order if k in scenario_powercap]
    else:
        ordered_rittal = list(scenario_rittal.items())
        ordered_powercap = list(scenario_powercap.items())

    # Combine all runs for each scenario (only include scenarios with data)
    rittal_data = []
    rittal_labels = []
    for scenario_key, runs in ordered_rittal:
        if runs:
            combined = pd.concat(runs, ignore_index=True)
            rittal_data.append(combined)
            rittal_labels.append(scenario_labels.get(scenario_key, scenario_key))

    powercap_data = []
    powercap_labels = []
    for scenario_key, runs in ordered_powercap:
        if runs:
            combined = pd.concat(runs, ignore_index=True)
            powercap_data.append(combined)
            powercap_labels.append(scenario_labels.get(scenario_key, scenario_key))

    if not (rittal_data or powercap_data):
        print("No data found for plotting")
        return

    # Create figure with two y-axes and specify relative widths (1:3 ratio)
    fig = plt.figure(figsize=(12, 6))
    gs = fig.add_gridspec(2, 1)
    ax1 = fig.add_subplot(gs[0])  # EM plot (larger)
    ax2 = fig.add_subplot(gs[1])  # RAPL plot (smaller)


    # Plot Rittal data on the left subplot
    if rittal_data:
        bp2 = ax1.boxplot(rittal_data, patch_artist=True,medianprops=dict(color="grey", linewidth=1.5),
                          showmeans=True,
                          meanprops={"marker":"x",
                                     "markerfacecolor":"red",
                                     "markeredgecolor":"red",
                                     "markersize":5}
                          )
        ax1.set_xticklabels(rittal_labels, rotation=0, ha='center')
        ax1.tick_params(axis='x', pad=10)  # Erhöht den Abstand zwischen Plot und Labels
        ax1.set_ylabel('Power (Watts)', color='black')
        ax1.tick_params(axis='y', labelcolor='black')
        # Add grid lines
        ax1.yaxis.grid(True, linestyle='--', alpha=0.7)
        # Color the boxes
        for patch in bp2['boxes']:
            patch.set_facecolor('lightgrey')
        ax1.set_title('Idle Power Consumption $P_{EM}$')

    # Plot RAPL data on the right subplot
    if powercap_data:
        bp1 = ax2.boxplot(powercap_data, patch_artist=True,medianprops=dict(color="grey", linewidth=1.5),
                          showmeans=True,
                          meanprops={"marker":"x",
                                     "markerfacecolor":"white",
                                     "markeredgecolor":"red",
                                     "markersize":5}
                          )
        ax2.set_xticklabels(powercap_labels, rotation=0, ha='center')
        ax2.tick_params(axis='x', pad=10)  # Erhöht den Abstand zwischen Plot und Labels

        ax2.set_ylabel('Power (Watts)', color='black')
        ax2.tick_params(axis='y', labelcolor='black')
        # Add grid lines
        ax2.yaxis.grid(True, linestyle='--', alpha=0.7)
        # Color the boxes
        for patch in bp1['boxes']:
            patch.set_facecolor('lightgrey')
        ax2.set_title('Idle Power Consumption $P_{S}$')

    # plt.suptitle('Idle Power Consumption')
    plt.tight_layout()

    # Save plot
    plt.savefig(output_path, format='pdf', bbox_inches='tight')
    #plt.show()
    plt.close()

    # Print statistics
    print("\Statistics:")
    print("-" * 50)

    if rittal_data:
        print("\nEM (Rittal) Measurements:")
        print("-" * 20)
        for data, label in zip(rittal_data, rittal_labels):
            mean_value = np.mean(data)
            median_value = np.median(data)
            print(f"\n{label}:")
            print(f"Mean: {mean_value:.2f} Watt")
            print(f"Median: {median_value:.2f} Watt")

    if powercap_data:
        print("\nRAPL Measurements:")
        print("-" * 20)
        for data, label in zip(powercap_data, powercap_labels):
            mean_value = np.mean(data)
            median_value = np.median(data)
            print(f"\n{label}:")
            print(f"Mean: {mean_value:.2f} Watt")
            print(f"Median: {median_value:.2f} Watt")


# Automatically collect all run folders (0, 0_run2, 0_run3, …) and all scenario subfolders.
# Uses the shared helper so we find directories both flat and nested (Container/VM/).

exp_results = Path(__file__).resolve().parent
env_names = discover_environments(exp_results)
if not env_names:
    env_names = [None]

custom_labels_by_suffix = {
    "idle_no_tools": "Idle",
    "docker_none": "TS1/RS1 no measurements",
    "docker_idle": "TS1/RS1",
    "docker_scaphandre": "TS2/RS1",
    "docker_kepler": "TS3/RS1",
    "docker_powerapi": "TS4/RS1",
    "docker_joularjx": "TS5/RS1",
    "docker_otjae": "TS6/RS1"
    # Add more custom labels here
}

for env_name in env_names:
    env_key = env_name if env_name else 'flat'
    load_level_map = build_run_dirs(exp_results, environment=env_name) if env_name else build_run_dirs(exp_results)

    data_directories = []
    for run_folder in load_level_map.get("0", []):
        for scenario_dir in run_folder.iterdir():
            if scenario_dir.is_dir():
                data_directories.append(str(scenario_dir))

    if data_directories:
        create_power_consumption_boxplot(data_directories, f"./idle_power_consumption_boxplot_{env_key}.pdf", custom_labels_by_suffix, trim_seconds=60)
        print(f"Environment {env_name}: Saved plot to ./idle_power_consumption_boxplot_{env_key}.pdf")
    else:
        print(f"Environment {env_name}: No idle power data found (load level 0)")
