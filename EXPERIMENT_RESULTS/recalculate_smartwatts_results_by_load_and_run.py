"""
recalculate_smartwatts_results_by_load_and_run.py

Iterate all load levels and PowerAPI scenarios in EXPERIMENT_RESULTS, remove the
existing smartwattsresults directory for each PowerAPI run, and rebuild it by
running the SmartWatts formula container over the downloaded PowerAPI sensor
reports.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from shared import build_run_dirs, discover_environments, scenario_matches


POWERAPI_CATEGORY = "powerapi"
REQUIRED_REPORT_FILES = ("core.csv", "msr.csv", "rapl.csv")


@dataclass
class ScenarioResult:
    load_level: str
    run_dir: Path
    scenario_dir: Path
    status: str
    details: str = ""


# Replaced by shared.build_run_dirs + discover_environments


def iter_powerapi_scenarios(root: Path, *, environment: str | None = None):
    """Yield (env_name, load_level, run_dir, scenario_dir) for all PowerAPI scenarios."""
    load_level_map = build_run_dirs(root, environment=environment)
    yield_str = "env=%s," if environment else "%s, "

    for load_level in sorted(load_level_map.keys(), key=int):
        for run_dir in sorted(load_level_map[load_level]):
            for scenario_dir in sorted(p for p in run_dir.iterdir() if p.is_dir()):
                if scenario_matches(scenario_dir.name, POWERAPI_CATEGORY):
                    yield (environment or "unknown"), load_level, run_dir, scenario_dir


def validate_required_reports(scenario_dir: Path) -> list[str]:
    """Return a list of missing PowerAPI report filenames."""
    reports_dir = scenario_dir / "powerapi-sensor-reporting"
    return [name for name in REQUIRED_REPORT_FILES if not (reports_dir / name).is_file()]


def build_smartwatts_command(base_dir: Path) -> list[str]:
    """Build the SmartWatts Podman command for one scenario directory."""
    return [
        "docker", "run", "--rm",
        "-v", f"{base_dir.resolve()}:/work",
        "powerapi/smartwatts-formula",
        "--verbose",
        "--input", "csv",
        "--files", "/work/powerapi-sensor-reporting/core.csv,/work/powerapi-sensor-reporting/msr.csv,/work/powerapi-sensor-reporting/rapl.csv",
        "--model", "HWPCReport",
        "--output", "csv",
        "--directory", "/work/smartwattsresults",
        "--cpu-base-freq", "2000",
        "--cpu-error-threshold", "2.0",
        "--disable-dram-formula",
        "--sensor-reports-frequency", "1000",
    ]


def podman_is_available() -> bool:
    """Return True when podman can be invoked from PATH."""
    try:
        result = subprocess.run(
            ["podman", "--version"],
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        return False

    return result.returncode == 0


def recalculate_smartwatts_for_scenario(
    load_level: str,
    run_dir: Path,
    scenario_dir: Path,
    dry_run: bool = False,
    force_remove: bool = True,
) -> ScenarioResult:
    """Delete and regenerate SmartWatts results for a single PowerAPI scenario."""
    smartwatts_dir = scenario_dir / "smartwattsresults"
    missing_reports = validate_required_reports(scenario_dir)
    if missing_reports:
        missing = ", ".join(missing_reports)
        return ScenarioResult(
            load_level=load_level,
            run_dir=run_dir,
            scenario_dir=scenario_dir,
            status="skipped",
            details=f"missing PowerAPI report files: {missing}",
        )

    if smartwatts_dir.exists() and not force_remove:
        return ScenarioResult(
            load_level=load_level,
            run_dir=run_dir,
            scenario_dir=scenario_dir,
            status="skipped",
            details="smartwattsresults directory already exists (use --force-remove to replace)",
        )

    cmd = build_smartwatts_command(scenario_dir)

    if dry_run:
        removal_state = "would remove" if smartwatts_dir.exists() else "would create"
        return ScenarioResult(
            load_level=load_level,
            run_dir=run_dir,
            scenario_dir=scenario_dir,
            status="dry-run",
            details=f"{removal_state} {smartwatts_dir} and run: {' '.join(cmd)}",
        )

    try:
        if smartwatts_dir.exists():
            shutil.rmtree(smartwatts_dir)

        result = subprocess.run(cmd, capture_output=True, text=True)
    except FileNotFoundError:
        return ScenarioResult(
            load_level=load_level,
            run_dir=run_dir,
            scenario_dir=scenario_dir,
            status="failed",
            details="podman executable not found in PATH",
        )
    except Exception as exc:  # pragma: no cover - defensive runtime safeguard
        return ScenarioResult(
            load_level=load_level,
            run_dir=run_dir,
            scenario_dir=scenario_dir,
            status="failed",
            details=str(exc),
        )

    if result.returncode != 0:
        details = (result.stderr or result.stdout or "SmartWatts processing failed").strip()
        return ScenarioResult(
            load_level=load_level,
            run_dir=run_dir,
            scenario_dir=scenario_dir,
            status="failed",
            details=details,
        )

    power_report_files = list(smartwatts_dir.rglob("PowerReport.csv"))
    if not power_report_files:
        return ScenarioResult(
            load_level=load_level,
            run_dir=run_dir,
            scenario_dir=scenario_dir,
            status="failed",
            details="SmartWatts command completed but no PowerReport.csv files were generated",
        )

    return ScenarioResult(
        load_level=load_level,
        run_dir=run_dir,
        scenario_dir=scenario_dir,
        status="recalculated",
        details=(result.stdout or "").strip(),
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Remove and regenerate smartwattsresults for all PowerAPI "
            "scenarios under EXPERIMENT_RESULTS."
        )
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the actions that would be taken without deleting or rebuilding results.",
    )
    parser.add_argument(
        "--force-remove",
        action="store_true",
        help="Force remove existing smartwattsresults directories and rebuild. By default, existing directories are skipped.",
    )
    parser.add_argument(
        "--stop-on-error",
        action="store_true",
        help="Stop after the first scenario that fails to recalculate.",
    )
    parser.add_argument(
        "--environment",
        type=str,
        default=None,
        help="Filter to a specific environment name. When omitted, all environments are processed.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    root = Path(__file__).resolve().parent

    if not args.dry_run and not podman_is_available():
        print("podman executable not found in PATH.")
        return 1

    scenarios = list(iter_powerapi_scenarios(root, environment=args.environment))
    if not scenarios:
        print("No PowerAPI scenarios found under EXPERIMENT_RESULTS.")
        return 0

    processed = 0
    recalculated = 0
    skipped = 0
    failed = 0

    for env_name, load_level, run_dir, scenario_dir in scenarios:
        print(f"\nProcessing: {scenario_dir}")
        result = recalculate_smartwatts_for_scenario(
            load_level=load_level,
            run_dir=run_dir,
            scenario_dir=scenario_dir,
            dry_run=args.dry_run,
        )
        processed += 1

        print(
            f"load={result.load_level} run={result.run_dir.name} "
            f"scenario={result.scenario_dir.name} status={result.status}"
        )
        if result.details:
            print(f"  {result.details}")

        if result.status == "recalculated":
            recalculated += 1
        elif result.status == "skipped":
            skipped += 1
        elif result.status == "failed":
            failed += 1
            if args.stop_on_error:
                break

    print(
        "Summary: "
        f"processed={processed} recalculated={recalculated} skipped={skipped} failed={failed}"
    )

    if failed > 0:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

