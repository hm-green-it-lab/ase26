"""
count_jmeter_failures_by_load_and_tool.py

Iterate all load levels and tool scenarios in EXPERIMENT_RESULTS, inspect only
JMeter .jtl files inside jmeter-result* directories, and print the count of
failed rows (success=false) per file.
"""

from __future__ import annotations

import csv
from pathlib import Path

from shared import build_run_dirs, discover_environments, scenario_matches


def _matches_category(scenario_dir_name: str) -> bool:
    return scenario_matches(scenario_dir_name, "kepler") or \
           scenario_matches(scenario_dir_name, "scaphandre") or \
           scenario_matches(scenario_dir_name, "otjae") or \
           scenario_matches(scenario_dir_name, "joularjx") or \
           scenario_matches(scenario_dir_name, "tools") or \
           scenario_matches(scenario_dir_name, "powerapi")


def normalize_success_value(value: str | None) -> str:
    """Normalize success field values for robust false matching."""
    if value is None:
        return ""
    return value.strip().strip('"').strip("'").lower()


def count_false_success_rows(jtl_file: Path, trim_seconds: float = 60,) -> int:
    """Count rows where success is false in a JMeter .jtl file."""
    fail_count = 0

    with jtl_file.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames or "success" not in reader.fieldnames:
            print(f"WARN: missing 'success' column in {jtl_file}")
            return 0
        
        rows = list(reader)

        if "timeStamp" in reader.fieldnames and trim_seconds > 0:
            timestamps = [
                int(r["timeStamp"])
                for r in rows
                if r.get("timeStamp")
            ]
            if timestamps:
                start = min(timestamps) + trim_seconds * 1000
                end = max(timestamps) - trim_seconds * 1000
                rows = [
                    r for r in rows
                    if start <= int(r["timeStamp"]) <= end
                ]
        for row in rows:
            success_value = normalize_success_value(row.get("success"))
            if success_value in {"false", "0", "no"}:
                fail_count += 1
                
    return fail_count


def main() -> None:
    root = Path(__file__).resolve().parent
    env_names = discover_environments(root)
    if not env_names:
        # Fall back to flat dirs (legacy)
        load_level_map = build_run_dirs(root)
        _process(load_level_map)
        return

    processed_files = 0
    for env_name in env_names:
        print(f"\n{'='*60}")
        print(f"Environment: {env_name}")
        print(f"{'='*60}")
        load_level_map = build_run_dirs(root, environment=env_name)
        if not load_level_map:
            print(f"WARNING: No load-level directories found in {env_name}/")
            continue
        processed_files += _process(load_level_map, env_name)

    if processed_files == 0:
        print("No .jtl files found under jmeter-result* directories for configured scenarios.")


def _process(
    load_level_map: dict[str, list[Path]],
    env_name: str = "",
) -> int:
    """Inner worker: iterate load levels and scenarios, print failures."""
    processed_files = 0
    header_prefix = f"[{env_name}] " if env_name else ""

    for load_level in sorted(load_level_map.keys(), key=int):
        for run_dir in load_level_map[load_level]:
            for scenario_dir in sorted(p for p in run_dir.iterdir() if p.is_dir()):
                if not _matches_category(scenario_dir.name):
                    continue

                jmeter_result_dirs = [
                    p for p in scenario_dir.rglob("jmeter-result*") if p.is_dir()
                ]

                for jmeter_result_dir in sorted(jmeter_result_dirs):
                    for jtl_file in sorted(jmeter_result_dir.rglob("*.jtl")):
                        fail_count = count_false_success_rows(jtl_file)
                        processed_files += 1
                        if fail_count > 0:
                            print(
                                f"{header_prefix}load={load_level} scenario={scenario_dir.name} "
                                f"file={jtl_file} fails={fail_count}"
                            )
    return processed_files


if __name__ == "__main__":
    main()

