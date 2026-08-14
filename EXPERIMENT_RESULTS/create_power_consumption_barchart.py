"""
create_power_consumption_barchart.py

Utilities to load power measurement files (Rittal EM and Intel RAPL/powercap), aggregate them per load level and tool, and produce a combined bar chart.

It also contains static values in the SCENARIO_CONSTANTS to avoid a recomputation of the transaction-level results.

This file is intended for offline analysis of experiment folders arranged under numeric load-level directories (e.g. `./230/`). It expects CSVs with certain column names as produced by the measurement tooling.
"""

import pandas as pd
import numpy as np
import matplotlib.pyplot as plt
import seaborn as sns
from pathlib import Path

from shared import load_power_data, summarize_repetitions, fmt_mean_std

def create_power_consumption_barchart(load_levels, output_path, trim_seconds=0):
    """
    Creates a combined bar chart of power consumption for different load levels,
    with RAPL and Rittal data side by side.
    Only considers files from directories ending with '_tools'.
    """
    # Constants for different scenarios: {load: (mean_watts, std_watts)}.
    # Point estimates (with std) hand-copied from the regenerated Container-,
    # Process-, and Transaction-level tables/scripts -- see those scripts for
    # the underlying per-run computation. Kepler/Scaphandre/PowerAPI use the
    # Container-level table (identical to their Process-level values, since
    # only one process runs per container in RS1). std=None means no error
    # bar is drawn for that point (used for the two Transaction-level sums
    # below, pending a fresh run of visualizeLoadLevelTransactionPowerConsumptionAsBoxplots.py
    # after its new Sum(P_T) mean+-std print was added).
    SCENARIO_CONSTANTS = {
        'Kepler (Container/Process)': {
            0: (0, 0),
            230: (56.56, 0.18),
            350: (104.63, 3.18),
            480: (198.37, 8.08),
            560: (221.12, 0.89),
        },
        'Scaphandre (Container/Process)': {
            0: (0, 0),
            230: (54.72, 4.15),
            350: (110.95, 8.01),
            480: (190.21, 5.52),
            560: (221.21, 0.81),
        },
        'PowerAPI (Container/Process)': {
            0: (0, 0),
            230: (188.85, 2.44),
            350: (219.63, 2.62),
            480: (245.10, 2.07),
            560: (251.15, 1.42),
        },
        'OTJAE (Process)': {
            0: (0, 0),
            230: (141.40, 2.90),
            350: (176.07, 1.25),
            480: (207.50, 0.44),
            560: (236.97, 4.41),
        },
        'JoularJX (Process)': {
            0: (0, 0),
            230: (182.23, 3.56),
            350: (207.63, 1.59),
            480: (222.48, 0.80),
            560: (239.27, 0.25),
        },
        # Sum(P_T) per load level (Container), from the "Sum(P_T) per load
        # level" print added to visualizeLoadLevelTransactionPowerConsumptionAsBoxplots.py.
        'OTJAE (Transaction)': {
            0: (0, 0),
            230: (139.59, 2.92),
            350: (174.06, 1.19),
            480: (204.87, 0.54),
            560: (233.91, 4.32),
        },
        'JoularJX (Transaction)': {
            0: (0, 0),
            230: (181.76, 3.59),
            350: (207.19, 1.61),
            480: (222.05, 0.81),
            560: (238.95, 0.29),
        }
    }

    from shared import build_run_dirs

    exp_results = Path(__file__).resolve().parent
    load_level_map = build_run_dirs(exp_results, environment="Container")

    data = {
        'Load': [],
        'Type': [],
        'Tool': [],
        'Power': [],
        'Std': [],
    }

    for load_level in load_levels:
        # Search all run folders that start with the load level (e.g., 230, 230_run2, 230_run3)
        run_dirs = load_level_map.get(str(load_level), [])

        if not run_dirs:
            print(f"No run directories starting with {load_level} found.")
            continue

        # Collect all experiment directories ending with '_tools' -- but NOT
        # '..._no_tools' (e.g. "baseline_idle_no_tools"), which also ends
        # with "_tools" and would otherwise be silently merged into the
        # same "tools" bucket as "docker_tools"/"vm_tools", inflating n and
        # conflating two different scenarios (pure idle vs. TS1/RS1 with
        # monitoring attached).
        def _is_baseline_tools_dir(name):
            """True for TS1 baseline dirs, excluding ``baseline_idle_no_tools``.

            A plain ``endswith('_tools')`` would also match ``no_tools``, which
            would merge pure idle measurements into the TS1 bucket and inflate
            n while conflating two different scenarios.
            """
            return name.endswith('_tools') and not name.endswith('no_tools')

        experiment_dirs = []
        for run_dir in run_dirs:
            if _is_baseline_tools_dir(run_dir.name):
                experiment_dirs.append(run_dir)

            found = [d for d in run_dir.glob("**/2025*") if d.is_dir() and _is_baseline_tools_dir(d.name)] + \
                    [d for d in run_dir.glob("**/2026*") if d.is_dir() and _is_baseline_tools_dir(d.name)]
            experiment_dirs.extend(found)

        if not experiment_dirs:
            print(f"No directories ending with '_tools' found for load {load_level}")
            continue

        for exp_dir in experiment_dirs:
            # Extract tool name from directory name
            tool_name = exp_dir.name.split('_')[-1]

            # Search for Rittal and Powercap files
            rittal_files = list(exp_dir.glob('**/rittal_*.csv'))
            powercap_files = list(exp_dir.glob('**/powercap_*.csv'))

            for r_file in rittal_files:
                try:
                    power_data = load_power_data(str(r_file), trim_seconds)
                    # One file per repetition, so this mean is already a
                    # single per-run value; std across repetitions is
                    # computed later via summarize_repetitions once all
                    # three runs' rows are collected.
                    mean_power = power_data['Power (Watts)'].mean()
                    data['Load'].append(load_level)
                    data['Type'].append('Rittal')
                    data['Tool'].append(tool_name)
                    data['Power'].append(mean_power)
                    data['Std'].append(np.nan)
                except Exception as e:
                    print(f"Error loading {r_file}: {e}")

            for p_file in powercap_files:
                try:
                    power_data = load_power_data(str(p_file), trim_seconds)
                    mean_power = power_data['Power'].mean()
                    data['Load'].append(load_level)
                    data['Type'].append('RAPL')
                    data['Tool'].append(tool_name)
                    data['Power'].append(mean_power)
                    data['Std'].append(np.nan)
                except Exception as e:
                    print(f"Error loading {p_file}: {e}")

    # Add constant scenario data: these are already-aggregated (mean, std)
    # point estimates (one row per load level, not one row per repetition).
    for scenario, load_values in SCENARIO_CONSTANTS.items():
        for load_level in load_levels:
            if load_level in load_values:
                mean_power, std_power = load_values[load_level]
                data['Load'].append(load_level)
                data['Type'].append('Scenario')
                data['Tool'].append(scenario)
                data['Power'].append(mean_power)
                data['Std'].append(std_power if std_power is not None else np.nan)

    # Create DataFrame from collected data
    df = pd.DataFrame(data)

    if df.empty:
        print("Warning: No data collected. Please check if the directories and files exist.")
        return

    # --- Color palette and tool mapping (from visualizePowerCapAsBoxplot.py) ---
    run_order = ["none", "OTJAE", "JoularJX", "Scaphandre", "Kepler", "PowerAPI"]
    tool_palette = dict(zip(run_order, sns.color_palette("Set2", n_colors=len(run_order))))
    em_color = "#888888"  # New color for EM (Rittal)

    def _measured_mean_std_per_load(subset_df, loads):
        """Per-load-level mean+-std from raw per-run rows (one row per
        repetition), via summarize_repetitions -- matches the mean-of-
        per-run-means convention used throughout the paper. Std falls back
        to 0.0 (not NaN) when unavailable, so it is always safe to pass to
        matplotlib's yerr."""
        means, stds = [], []
        for load in loads:
            vals = subset_df[subset_df['Load'] == load]['Power'].dropna().tolist()
            if not vals:
                means.append(np.nan)
                stds.append(0.0)
                continue
            stats = summarize_repetitions(vals)
            means.append(stats['mean'])
            stds.append(stats['std'] if stats['std'] is not None else 0.0)
        return means, stds

    def _scenario_mean_std_per_load(subset_df, loads):
        """Per-load-level (mean, std) for scenario constants, which are
        already-aggregated single point estimates (one row per load level,
        not one row per repetition)."""
        means, stds = [], []
        for load in loads:
            row = subset_df[subset_df['Load'] == load]
            if row.empty:
                means.append(np.nan)
                stds.append(0.0)
                continue
            means.append(row['Power'].iloc[0])
            std_val = row['Std'].iloc[0]
            stds.append(0.0 if pd.isna(std_val) else std_val)
        return means, stds

    fig, ax = plt.subplots(figsize=(18, 10))
    measured_tools = sorted([t for t in df['Tool'].unique() if t not in SCENARIO_CONSTANTS.keys()])
    scenario_tools = sorted([t for t in df['Tool'].unique() if t in SCENARIO_CONSTANTS.keys()])
    all_tools = measured_tools + scenario_tools
    x = np.arange(len(load_levels)) * 3.5
    total_width = 3.3
    total_bars = len(measured_tools) * 2 + len(scenario_tools)
    bar_width = total_width / total_bars
    bar_position = 0

    # Plot bars for measured tools
    for tool in measured_tools:
        tool_data = df[df['Tool'] == tool]
        # Rittal data (EM)
        rittal_data = tool_data[tool_data['Type'] == 'Rittal']
        rittal_means, rittal_stds = _measured_mean_std_per_load(rittal_data, load_levels)
        pos_rittal = x + bar_position * bar_width - total_width/2
        bars_rittal = ax.bar(pos_rittal, rittal_means, bar_width,
                             yerr=rittal_stds, capsize=3, error_kw={'linewidth': 1},
                             label=f'EM',
                             color=em_color,
                             hatch='',
                             edgecolor='black',
                             linewidth=0.5)
        for i, (bar, value, std) in enumerate(zip(bars_rittal, rittal_means, rittal_stds)):
            if not np.isnan(value) and value > 0:
                ax.text(bar.get_x() + bar.get_width()/2, bar.get_height() + std + 5,
                        '100%', ha='center', va='bottom', fontsize=10, fontweight='bold', rotation=90)
        bar_position += 1
        # RAPL data
        rapl_data = tool_data[tool_data['Type'] == 'RAPL']
        rapl_means, rapl_stds = _measured_mean_std_per_load(rapl_data, load_levels)
        pos_rapl = x + bar_position * bar_width - total_width/2
        # Map tool to run_order for color
        color = tool_palette.get(tool, '#333333')
        bars_rapl = ax.bar(pos_rapl, rapl_means, bar_width,
                           yerr=rapl_stds, capsize=3, error_kw={'linewidth': 1},
                           label=f'RAPL',
                           color=color,
                           hatch='////',
                           edgecolor='black',
                           linewidth=0.5)
        for i, (bar, rapl_val, rittal_val, std) in enumerate(zip(bars_rapl, rapl_means, rittal_means, rapl_stds)):
            if not np.isnan(rapl_val) and not np.isnan(rittal_val) and rittal_val > 0:
                percentage = (rapl_val / rittal_val) * 100
                ax.text(bar.get_x() + bar.get_width()/2, bar.get_height() + std + 5,
                        f'{percentage:.1f}%', ha='center', va='bottom', fontsize=10, fontweight='bold', rotation=90)
        bar_position += 1

    # Scenario hatches for (Process)/(Transaction) distinction
    scenario_hatches = {'Process': '', 'Transaction': '///'}
    # Plot bars for scenario constants
    for idx, scenario in enumerate(scenario_tools):
        scenario_data = df[df['Tool'] == scenario]
        scenario_means, scenario_stds = _scenario_mean_std_per_load(scenario_data, load_levels)
        pos_scenario = x + bar_position * bar_width - total_width/2
        # Determine base tool and type
        base_tool = None
        scenario_type = ''
        for t in run_order:
            if scenario.lower().startswith(t.lower()):
                base_tool = t
                break
        if '(Transaction)' in scenario:
            scenario_type = 'Transaction'
        elif '(Process)' in scenario:
            scenario_type = 'Process'
        color = tool_palette.get(base_tool, '#333333')
        hatch_pattern = scenario_hatches.get(scenario_type, '')
        bars_scenario = ax.bar(pos_scenario, scenario_means, bar_width,
                               yerr=scenario_stds, capsize=3, error_kw={'linewidth': 1},
                               label=f'{scenario}',
                               color=color,
                               edgecolor='black',
                               linewidth=1.5,
                               hatch=hatch_pattern)
        for i, (bar, scenario_val, std) in enumerate(zip(bars_scenario, scenario_means, scenario_stds)):
            if not np.isnan(scenario_val) and scenario_val > 0:
                load = load_levels[i]
                rittal_values = df[(df['Load'] == load) & (df['Type'] == 'Rittal')]['Power']
                if not rittal_values.empty:
                    rittal_mean = rittal_values.mean()
                    if rittal_mean > 0:
                        percentage = (scenario_val / rittal_mean) * 100
                        ax.text(bar.get_x() + bar.get_width()/2, bar.get_height() + std + 5,
                                f'{percentage:.1f}%', ha='center', va='bottom', fontsize=10, fontweight='bold', rotation=90)
        bar_position += 1

    # Chart labeling and formatting
    ax.set_xlabel('Load (T/s)', fontsize=16)
    ax.set_ylabel('Power (Watts)', fontsize=16)
    #ax.set_title('Power Consumption by Tool and Measurement Method', fontsize=16)
    ax.set_xticks(x)
    ax.set_xticklabels([str(int(l)*3) for l in load_levels], fontsize=14)
    ax.tick_params(axis='y', labelsize=14)
    ax.legend(loc='upper left', fontsize=14)
    ax.grid(True, alpha=0.3)

    # Add extra space at the top for percentage labels
    y_max = ax.get_ylim()[1]
    ax.set_ylim(top=y_max * 1.08)

    # Print statistics
    print("\nStatistics:")
    for load in load_levels:
        print(f"\nLoad level: {load} Requests/Second")
        load_data = df[df['Load'] == load]

        for tool in measured_tools:
            tool_data = load_data[load_data['Tool'] == tool]
            if not tool_data.empty:
                print(f"\n{tool}:")
                for measurement_type in ['Rittal', 'RAPL']:
                    type_data = tool_data[tool_data['Type'] == measurement_type]
                    if not type_data.empty:
                        # Each row here is already one repetition's per-run
                        # mean (one file per run directory), so summarizing
                        # this list directly gives mean/std/CI across n=3 reps.
                        stats = summarize_repetitions(type_data['Power'].tolist())
                        print(f"  {measurement_type}: {fmt_mean_std(stats['mean'], stats['std'])} (n={stats['n']})")

        # Print scenario constants (hand-copied point estimates from other
        # scripts' regenerated outputs, including their std where available)
        for scenario in scenario_tools:
            scenario_data = load_data[load_data['Tool'] == scenario]
            if not scenario_data.empty:
                mean_power = scenario_data['Power'].iloc[0]
                std_power = scenario_data['Std'].iloc[0]
                std_power = None if pd.isna(std_power) else std_power
                print(f"\n{scenario.upper()} (Scenario): {fmt_mean_std(mean_power, std_power)}")

    plt.tight_layout()
    plt.savefig(output_path, format='pdf', bbox_inches='tight')
    plt.close()

    # --- Print differences between transaction and process values for JOULARJX and OTJAE ---
    print("\nDifferences (Transaction - Process):")
    tool_names = {"joularjx": "JoularJX", "otjae": "OTJAE"}
    for tool in ["joularjx", "otjae"]:
        name = tool_names[tool]
        process_key = f"{name} (Process)"
        transaction_key = f"{name} (Transaction)"
        tool_upper = name.upper()
        print(f"\n{tool_upper}:")
        for load in load_levels:
            process_pair = SCENARIO_CONSTANTS.get(process_key, {}).get(load, None)
            transaction_pair = SCENARIO_CONSTANTS.get(transaction_key, {}).get(load, None)
            if process_pair is not None and transaction_pair is not None:
                process_val = process_pair[0]
                transaction_val = transaction_pair[0]
                diff = transaction_val - process_val
                print(f"  Load {load}: {tool_upper} (Transaction) {transaction_val:.2f} - {tool_upper} (Process) {process_val:.2f} = {diff:.2f} W")
            else:
                print(f"  Load {load}: data missing")

if __name__ == "__main__":
    # Note the "../" in the output path: this script writes one level above the
    # current working directory and therefore has to be started from inside
    # EXPERIMENT_RESULTS/, not from the repository root.
    load_levels = [0, 230, 350, 480, 560]
    create_power_consumption_barchart(load_levels,
                                      "../power_consumption_combined_barchart.pdf",
                                      trim_seconds=60)