"""
statistical_appendix.py

Consolidated statistical appendix for cross-table claims that span multiple
result tables and don't naturally belong to any single table's footnote:

  1. Tool power (Kepler/Scaphandre/PowerAPI at container level) as a
     percentage of external-meter (EM/Rittal) ground truth, per environment
     and load level, with mean/std/CI and a session-paired Cohen's dz +
     exact Wilcoxon signed-rank test on the (tool - EM) gap. This directly
     backs the abstract's headline accuracy claim ("18-67% accuracy ...
     compared to external measurements"), which none of the per-table
     statistical notes cover directly (those compare tools against P_S/RAPL,
     not against EM).
  2. Container vs VM comparison for OTJAE and JoularJX process power, using
     an independent-sample Cohen's d (Container and VM are different
     physical sessions, not pairable run-for-run).

Reuses collect_data_by_load_level from the Container-level script (Kepler/
Scaphandre/PowerAPI + EM) and process_docker_otjae from the Process-level
script (OTJAE), plus shared.parse_joularjx_power/load_rittal_data (JoularJX/
EM), to avoid re-implementing file parsing.
"""

import numpy as np
import pandas as pd
from pathlib import Path
from collections import defaultdict

from shared import (
    build_run_dirs,
    discover_environments,
    get_jmeter_time_bounds,
    load_power_data,
    load_rittal_data,
    parse_joularjx_power,
    scenario_matches,
    summarize_repetitions,
    cohens_d_independent,
    mann_whitney_u_exact,
    fmt_mean_std,
    per_run_means,
)
from visualizeLoadLevelContainerPowerConsumptionAsBoxplots import (
    collect_data_by_load_level as collect_container_level_data,
)
from visualizeLoadLevelProcessPowerConsumptionAsBoxplots import process_docker_otjae


def _compute_pcpumin_pcpumax(exp_results, env_name, trim_seconds=60):
    """Same procedure as the Process-level script: mean P_S at the lowest
    (idle) and highest tool-scenario load levels, used to parameterize
    OTJAE's linear CPU power model."""
    load_level_map = build_run_dirs(exp_results, environment=env_name) if env_name else build_run_dirs(exp_results)
    pcpumin = pcpumax = None
    for ll in ['0', '560']:
        dirs = load_level_map.get(ll, [])
        vals = []
        for run_path in dirs:
            for scenario_dir in run_path.iterdir():
                if not scenario_dir.is_dir() or not scenario_matches(scenario_dir.name, 'tools'):
                    continue
                jb = get_jmeter_time_bounds(str(scenario_dir), trim_seconds)
                for fp in scenario_dir.glob('**/powercap_*.csv'):
                    try:
                        pdf = load_power_data(str(fp), trim_seconds, jb)
                        if 'Power' in pdf.columns:
                            vals.append(pdf['Power'].mean())
                    except Exception as e:
                        print(f"Error loading {fp}: {e}")
        if vals:
            mean_val = float(np.mean(vals))
            if ll == '0':
                pcpumin = mean_val
            else:
                pcpumax = mean_val
    return pcpumin, pcpumax


def _print_tool_vs_em_section(env_data):
    """Section 1: Kepler/Scaphandre/PowerAPI power as % of EM (Rittal),
    per environment and load level, plus a pooled independent-samples summary."""
    print("\n" + "=" * 70)
    print("SECTION 1: Tool power as % of EM (Rittal) ground truth")
    print("=" * 70)
    tools = ("kepler", "scaphandre", "powerapi")
    for env_key, data_by_load in env_data.items():
        numeric_items = [(k, v) for k, v in data_by_load.items() if k.isdigit()]
        sorted_loads = sorted(numeric_items, key=lambda x: int(x[0]))
        if not sorted_loads:
            continue
        session_pcts = {t: [] for t in tools}
        session_tool_vals = {t: [] for t in tools}
        session_em_vals = {t: [] for t in tools}
        print(f"\n--- Environment: {env_key} ---")
        for load_level, scenario_dict in sorted_loads:
            em_by_run = per_run_means(scenario_dict, 'tools', 'rittal')
            print(f"\n  Load {int(load_level) * 3}T/s:")
            for tool in tools:
                tool_by_run = per_run_means(scenario_dict, tool, tool)
                common_runs = sorted(set(tool_by_run) & set(em_by_run))
                if not common_runs:
                    continue
                pcts = [tool_by_run[r] / em_by_run[r] * 100 for r in common_runs if em_by_run[r] != 0]
                pct_stats = summarize_repetitions(pcts)
                session_pcts[tool].extend(pcts)
                session_tool_vals[tool].extend(tool_by_run.values())
                session_em_vals[tool].extend(em_by_run.values())
                print(f"    {tool}: {fmt_mean_std(pct_stats['mean'], pct_stats['std'], unit='%')} of EM (n={pct_stats['n']})")

        print(f"\n  --- Pooled across all load levels (independent samples) ---")
        for tool in tools:
            if not session_pcts[tool]:
                continue
            pooled_pct = summarize_repetitions(session_pcts[tool])
            d_indep = cohens_d_independent(session_tool_vals[tool], session_em_vals[tool])
            mw = mann_whitney_u_exact(session_tool_vals[tool], session_em_vals[tool])
            print(f"    {tool}: {fmt_mean_std(pooled_pct['mean'], pooled_pct['std'], unit='%')} of EM overall (n={pooled_pct['n']})")
            if d_indep is not None:
                print(f"      Cohen's d (tool vs EM, independent) = {d_indep:.3f}")
            if mw["p_two_sided"] is not None:
                print(f"      Mann-Whitney U vs EM: U={mw['statistic']:.1f}, p={mw['p_two_sided']:.4f} (p_floor={mw['p_floor']:.4f})")


def _collect_process_tool_by_env(exp_results, env_names, trim_seconds=60, included_load_levels=None):
    """OTJAE and JoularJX process power (P_P), per environment/load level, as
    ``{env: {load_level: {'otjae': {run_label: mean}, 'joularjx': {...}, 'rittal': {...}}}}``.

    Only load levels listed in *included_load_levels* are collected (all of them
    when it is ``None``). The idle level is normally excluded so that this
    section covers the same load levels as the process-level tables in the
    paper; pooling idle in would otherwise dominate the comparison, because at
    idle the attributed process power is a small fraction of the values seen
    under load. Note that pcpumin/pcpumax are always derived from the full data
    set, since the idle level defines P_CPUmin.
    """
    result = {}
    for env_name in env_names:
        pcpumin, pcpumax = _compute_pcpumin_pcpumax(exp_results, env_name, trim_seconds)
        load_level_map = build_run_dirs(exp_results, environment=env_name) if env_name else build_run_dirs(exp_results)
        by_load = defaultdict(lambda: {'otjae': defaultdict(list), 'joularjx': defaultdict(list), 'rittal': defaultdict(list)})
        for load_level, dirs in load_level_map.items():
            if not load_level.isdigit():
                continue
            if included_load_levels is not None and load_level not in included_load_levels:
                continue
            for run_path in dirs:
                run_label = run_path.name
                for scenario_dir in run_path.iterdir():
                    if not scenario_dir.is_dir():
                        continue
                    scenario_name = scenario_dir.name
                    jb = get_jmeter_time_bounds(str(scenario_dir), trim_seconds)
                    if scenario_matches(scenario_name, 'tools'):
                        for fp in scenario_dir.glob('**/rittal_*.csv'):
                            try:
                                pdf = load_rittal_data(str(fp), trim_seconds, jb)
                                if 'Power (Watts)' in pdf.columns:
                                    by_load[load_level]['rittal'][run_label].append(pdf['Power (Watts)'])
                            except Exception as e:
                                print(f"Error loading {fp}: {e}")
                    if scenario_matches(scenario_name, 'joularjx'):
                        try:
                            jd = parse_joularjx_power(scenario_dir, trim_seconds=trim_seconds, jmeter_bounds=jb)
                            if not jd.empty:
                                by_load[load_level]['joularjx'][run_label].append(jd['Power'])
                        except Exception as e:
                            print(f"Error loading joularjx: {e}")
                    if scenario_matches(scenario_name, 'otjae') and pcpumin is not None and pcpumax is not None:
                        try:
                            op = process_docker_otjae(scenario_dir, trim_seconds, pcpumin, pcpumax)
                            if op is not None and not op.empty:
                                by_load[load_level]['otjae'][run_label].append(op)
                        except Exception as e:
                            print(f"Error loading otjae: {e}")
        # Collapse per-run lists (one file per run, typically) to per-run means.
        collapsed = {}
        for load_level, kinds in by_load.items():
            collapsed[load_level] = {}
            for kind, by_run in kinds.items():
                collapsed[load_level][kind] = {
                    r: float(pd.concat(v, ignore_index=True).mean())
                    for r, v in by_run.items() if v
                }
        result[env_name if env_name else 'flat'] = collapsed
    return result


def _print_container_vs_vm_section(process_data):
    """Section 2: Container vs VM comparison (independent-sample Cohen's d)
    for OTJAE and JoularJX process power, pooled across load levels."""
    print("\n" + "=" * 70)
    print("SECTION 2: Container vs VM process power (independent samples)")
    print("=" * 70)
    if "Container" not in process_data or "VM" not in process_data:
        print("  Container and/or VM data not available; skipping.")
        return
    for tool in ("joularjx", "otjae"):
        container_vals = []
        vm_vals = []
        for load_level, kinds in process_data["Container"].items():
            container_vals.extend(kinds.get(tool, {}).values())
        for load_level, kinds in process_data["VM"].items():
            vm_vals.extend(kinds.get(tool, {}).values())
        if not container_vals or not vm_vals:
            continue
        c_stats = summarize_repetitions(container_vals)
        v_stats = summarize_repetitions(vm_vals)
        d_indep = cohens_d_independent(container_vals, vm_vals)
        print(f"\n  {tool}:")
        print(f"    Container: {fmt_mean_std(c_stats['mean'], c_stats['std'])} (n={c_stats['n']})")
        print(f"    VM:        {fmt_mean_std(v_stats['mean'], v_stats['std'])} (n={v_stats['n']})")
        if d_indep is not None:
            print(f"    Cohen's d (independent, Container - VM) = {d_indep:.3f}")


def main():
    exp_results = Path(__file__).resolve().parent
    trim_seconds = 60
    scenario_suffixes = ["tools", "kepler", "scaphandre", "powerapi"]
    included_load_levels = ["230", "350", "480", "560"]

    env_names = [e for e in discover_environments(exp_results) if e in ("Container", "VM")]
    if not env_names:
        env_names = [None]

    container_level_data = collect_container_level_data(
        trim_seconds=trim_seconds,
        scenario_suffixes=scenario_suffixes,
        included_load_levels=included_load_levels,
        return_per_env=True,
        environments=env_names,
    )
    _print_tool_vs_em_section(container_level_data)

    process_data = _collect_process_tool_by_env(exp_results, env_names, trim_seconds, included_load_levels)
    _print_container_vs_vm_section(process_data)


if __name__ == "__main__":
    main()
