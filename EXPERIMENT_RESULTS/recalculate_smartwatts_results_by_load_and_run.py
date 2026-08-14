"""
recalculate_smartwatts_results_by_load_and_run.py

Iterate all load levels and PowerAPI scenarios in EXPERIMENT_RESULTS, show an
overview of which ones already have complete SmartWatts results and which
need (re)calculation, then rebuild the missing/incomplete ones by running
SmartWatts over the downloaded PowerAPI sensor reports -- up to --max-workers
scenarios at a time in parallel, since each SmartWatts run takes a long time.

Two execution engines are supported via --engine:
- "podman" (default): runs the containerized powerapi/smartwatts-formula
  image, bind-mounting the scenario directory.
- "wsl": runs the 'smartwatts' PyPI package directly inside a WSL2 distro
  (`pip install smartwatts` there first), with no container layer in
  between. This is only meaningful on Windows -- SmartWatts' actor IPC uses
  ZeroMQ's ipc:// transport (Unix domain sockets under a hardcoded /tmp),
  which native Windows Python cannot use at all, so this engine always shells
  out to `wsl.exe` rather than running smartwatts as a plain Windows process.
  Because that /tmp socket path is deterministic (derived only from the
  fixed actor names, not per-process), concurrent invocations on the same
  distro would collide, so --engine wsl always runs scenarios serially
  regardless of --max-workers.
"""

from __future__ import annotations

import argparse
import hashlib
import shutil
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path

from shared import build_run_dirs, scenario_matches


POWERAPI_CATEGORY = "powerapi"
REQUIRED_REPORT_FILES = ("core.csv", "msr.csv", "rapl.csv")
DEFAULT_MAX_WORKERS = 3
DEFAULT_WSL_PYTHON = "python3"


@dataclass
class ScenarioResult:
    """Outcome of one attempted SmartWatts recalculation.

    ``status`` is one of ``ok``, ``failed``, or ``skipped``; ``details`` carries
    the reason or error message shown in the final summary.
    """
    load_level: str
    run_dir: Path
    scenario_dir: Path
    status: str
    details: str = ""
    duration_seconds: float = 0.0


@dataclass
class ScenarioInfo:
    """One discovered PowerAPI scenario and its current result state.

    ``state`` records what the overview pass found — whether the scenario
    already has complete SmartWatts results or needs recalculating — so the
    work list can be printed before any of it is executed.
    """
    env_name: str
    load_level: str
    run_dir: Path
    scenario_dir: Path
    state: str
    details: str


def iter_powerapi_scenarios(root: Path, *, environment: str | None = None):
    """Yield (env_name, load_level, run_dir, scenario_dir) for all PowerAPI scenarios."""
    load_level_map = build_run_dirs(root, environment=environment)

    for load_level in sorted(load_level_map.keys(), key=int):
        for run_dir in sorted(load_level_map[load_level]):
            for scenario_dir in sorted(p for p in run_dir.iterdir() if p.is_dir()):
                if scenario_matches(scenario_dir.name, POWERAPI_CATEGORY):
                    yield (environment or "unknown"), load_level, run_dir, scenario_dir


def validate_required_reports(scenario_dir: Path) -> list[str]:
    """Return a list of missing PowerAPI report filenames."""
    reports_dir = scenario_dir / "powerapi-sensor-reporting"
    return [name for name in REQUIRED_REPORT_FILES if not (reports_dir / name).is_file()]


def find_energy_consumption_reports(scenario_dir: Path) -> list[Path]:
    """Return the ``PowerReport.csv`` files actually consumed downstream, by
    visualizeLoadLevelContainer/ProcessPowerConsumptionAsBoxplots.py: those
    nested under smartwattsresults' ``energy-consumption.slice`` subtree
    (those scripts glob ``energy.slice/**/docker-*.scope`` and ``vm-*.scope``
    report dirs, which live inside it). Plenty of other PowerReport.csv files
    exist elsewhere under smartwattsresults (e.g. for the HWPC sensor's own
    cgroup) that nothing downstream reads, so their mere presence doesn't
    indicate a usable result -- this is the set that does.

    RS2/RS3 scenarios nest an extra ``*-experiment-2.slice`` for the second
    container, so they legitimately return more files here than
    single-container scenarios; that's not itself a sign of a problem.
    """
    smartwatts_dir = scenario_dir / "smartwattsresults"
    return sorted(smartwatts_dir.glob("**/energy-consumption.slice/**/PowerReport.csv"))


def _count_file_lines(file_path: Path) -> int:
    """Count the lines in *file_path*, tolerating undecodable bytes.

    Used to judge whether a report actually holds data rather than merely
    existing, so truncated results are recognized as incomplete.
    """
    with open(file_path, "r", encoding="utf-8", errors="ignore") as fh:
        return sum(1 for _ in fh)


def _describe_energy_consumption_reports(reports: list[Path]) -> str:
    """Human-readable ``N file(s) (... lines)`` summary, content (line count)
    based rather than just presence, per file under energy-consumption.slice."""
    line_counts = [_count_file_lines(f) for f in reports]
    total_lines = sum(line_counts)
    lines_range = f"{min(line_counts)}-{max(line_counts)}" if len(line_counts) > 1 else str(line_counts[0])
    return (
        f"{len(reports)} PowerReport.csv file(s) under energy-consumption.slice "
        f"({total_lines} total lines, {lines_range} lines/file)"
    )


def classify_scenario(scenario_dir: Path, force_remove: bool) -> tuple[str, str]:
    """Classify one PowerAPI scenario's SmartWatts recalculation state.

    Returns ``(state, details)`` where state is one of:
    - ``"missing_input"``: required PowerAPI sensor report CSVs are missing,
      so the scenario cannot be processed at all.
    - ``"complete"``: a ``smartwattsresults`` directory already exists and
      contains at least one ``PowerReport.csv`` under ``energy-consumption.slice``
      (see :func:`find_energy_consumption_reports`) -- the *details* string
      reports each file's line count so a header-only/empty file is visible
      at a glance rather than only checking existence.
    - ``"needs_calculation"``: ``smartwattsresults`` is missing or has no
      such report, or *force_remove* is set (which forces a rebuild even of
      already-complete results).
    """
    missing_reports = validate_required_reports(scenario_dir)
    if missing_reports:
        return "missing_input", f"missing PowerAPI report files: {', '.join(missing_reports)}"

    smartwatts_dir = scenario_dir / "smartwattsresults"
    if not smartwatts_dir.exists():
        return "needs_calculation", "smartwattsresults directory does not exist"

    energy_reports = find_energy_consumption_reports(scenario_dir)
    if not energy_reports:
        return (
            "needs_calculation",
            "smartwattsresults directory exists but no PowerReport.csv files found under energy-consumption.slice",
        )

    summary = _describe_energy_consumption_reports(energy_reports)

    if force_remove:
        return "needs_calculation", f"--force-remove set; {summary} will be rebuilt"

    return "complete", f"{summary} already present"


def build_smartwatts_command(base_dir: Path) -> list[str]:
    """Build the SmartWatts Podman command for one scenario directory."""
    return [
        "podman", "run", "--rm",
        "-v", f"{base_dir.resolve()}:/work",
        "powerapi/smartwatts-formula:2.4.1",
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


def _staging_dir_name(scenario_dir: Path) -> str:
    """Deterministic, collision-safe staging directory name for one scenario."""
    digest = hashlib.blake2b(str(scenario_dir).encode("utf-8"), digest_size=8).hexdigest()
    return f"smartwatts-work-{digest}"


def _run_wsl(distro: str | None, shell_cmd: str) -> subprocess.CompletedProcess:
    """Run a POSIX shell command inside WSL2, optionally in a specific distro."""
    cmd = ["wsl.exe"]
    if distro:
        cmd += ["-d", distro]
    cmd += ["--", "sh", "-c", shell_cmd]
    return subprocess.run(cmd, capture_output=True, text=True)


def _windows_path_to_wsl(path: Path, distro: str | None) -> str:
    """Resolve an absolute Windows path to its WSL2-visible path (e.g. under /mnt/c/...)."""
    cmd = ["wsl.exe"]
    if distro:
        cmd += ["-d", distro]
    cmd += ["--", "wslpath", "-a", str(path.resolve())]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError((result.stderr or result.stdout or "wslpath failed").strip())
    return result.stdout.strip()


def wsl_smartwatts_is_available(distro: str | None, python_exe: str) -> bool:
    """Return True when *python_exe* inside WSL2 (optionally *distro*) can import smartwatts."""
    try:
        result = _run_wsl(distro, f"{python_exe} -c 'import smartwatts, powerapi'")
    except FileNotFoundError:
        return False
    return result.returncode == 0


def stage_scenario_into_wsl(scenario_dir: Path, distro: str | None) -> tuple[str | None, str]:
    """Bulk-copy this scenario's PowerAPI sensor reports into a native WSL2
    directory (under ``$HOME``), so a SmartWatts process running directly
    inside WSL2 -- no container in between -- reads them without crossing the
    Windows<->WSL2 filesystem boundary during the run itself.

    Returns ``(wsl_work_dir, error)`` -- *wsl_work_dir* is a Linux-side path
    string (e.g. ``"$HOME/smartwatts-work-<hash>"``), ``None`` on failure.
    """
    work_name = _staging_dir_name(scenario_dir)
    try:
        wsl_reports_dir = _windows_path_to_wsl(scenario_dir / "powerapi-sensor-reporting", distro)
    except RuntimeError as exc:
        return None, f"could not resolve WSL path: {exc}"

    wsl_work_dir = f"$HOME/{work_name}"
    copy_in = _run_wsl(
        distro,
        f'rm -rf "{wsl_work_dir}"; mkdir -p "{wsl_work_dir}/powerapi-sensor-reporting" && '
        f'cp -a "{wsl_reports_dir}/." "{wsl_work_dir}/powerapi-sensor-reporting/"',
    )
    if copy_in.returncode != 0:
        return None, (copy_in.stderr or copy_in.stdout or "staging copy-in failed").strip()

    return wsl_work_dir, ""


def unstage_wsl_results(wsl_work_dir: str, scenario_dir: Path, distro: str | None) -> str:
    """Bulk-copy ``smartwattsresults`` out of the WSL2 staging directory into
    *scenario_dir* and remove the staging directory. Returns an error string,
    empty on success."""
    smartwatts_dir = scenario_dir / "smartwattsresults"
    smartwatts_dir.mkdir(parents=True, exist_ok=True)
    error = ""
    try:
        wsl_dst = _windows_path_to_wsl(smartwatts_dir, distro)
        copy_out = _run_wsl(distro, f'cp -a "{wsl_work_dir}/smartwattsresults/." "{wsl_dst}/"')
        if copy_out.returncode != 0:
            error = (copy_out.stderr or copy_out.stdout or "staging copy-out failed").strip()
    except RuntimeError as exc:
        error = f"could not resolve WSL path: {exc}"
    _run_wsl(distro, f'rm -rf "{wsl_work_dir}"')
    return error


def build_smartwatts_wsl_shell_command(wsl_work_dir: str, python_exe: str) -> str:
    """Build the ``sh -c`` command line that runs SmartWatts directly inside
    WSL2 against a staged work directory (see :func:`stage_scenario_into_wsl`)."""
    files = ",".join(f"{wsl_work_dir}/powerapi-sensor-reporting/{name}" for name in REQUIRED_REPORT_FILES)
    return (
        f'{python_exe} -m smartwatts --verbose --input csv --files "{files}" '
        f'--model HWPCReport --output csv --directory "{wsl_work_dir}/smartwattsresults" '
        f'--cpu-base-freq 2000 --cpu-error-threshold 2.0 --disable-dram-formula '
        f'--sensor-reports-frequency 1000'
    )


def _recalculate_via_podman(
    load_level: str,
    run_dir: Path,
    scenario_dir: Path,
    smartwatts_dir: Path,
    start: float,
) -> ScenarioResult:
    """Recalculate one scenario with the containerized SmartWatts formula.

    Runs the ``powerapi/smartwatts-formula`` image under Podman, bind-mounting
    *scenario_dir* so the sensor reports are read and the results written back
    in place. *start* is the wall-clock time the attempt began, used to report
    the duration.
    """
    cmd = build_smartwatts_command(scenario_dir)
    try:
        result = subprocess.run(cmd, capture_output=True, text=True)
    except FileNotFoundError:
        return ScenarioResult(
            load_level=load_level, run_dir=run_dir, scenario_dir=scenario_dir,
            status="failed", details="podman executable not found in PATH",
            duration_seconds=time.perf_counter() - start,
        )
    except Exception as exc:  # pragma: no cover - defensive runtime safeguard
        return ScenarioResult(
            load_level=load_level, run_dir=run_dir, scenario_dir=scenario_dir,
            status="failed", details=str(exc),
            duration_seconds=time.perf_counter() - start,
        )

    duration = time.perf_counter() - start

    if result.returncode != 0:
        details = (result.stderr or result.stdout or "SmartWatts processing failed").strip()
        return ScenarioResult(
            load_level=load_level, run_dir=run_dir, scenario_dir=scenario_dir,
            status="failed", details=details, duration_seconds=duration,
        )

    energy_reports = find_energy_consumption_reports(scenario_dir)
    if not energy_reports:
        return ScenarioResult(
            load_level=load_level, run_dir=run_dir, scenario_dir=scenario_dir,
            status="failed",
            details="SmartWatts command completed but no PowerReport.csv files were generated under energy-consumption.slice",
            duration_seconds=duration,
        )

    return ScenarioResult(
        load_level=load_level, run_dir=run_dir, scenario_dir=scenario_dir,
        status="recalculated", details=(result.stdout or "").strip(), duration_seconds=duration,
    )


def _recalculate_via_wsl(
    load_level: str,
    run_dir: Path,
    scenario_dir: Path,
    smartwatts_dir: Path,
    start: float,
    distro: str | None,
    python_exe: str,
) -> ScenarioResult:
    """Recalculate one scenario with the ``smartwatts`` package inside WSL2.

    The sensor reports are first staged into a native WSL2 directory (see
    :func:`stage_scenario_into_wsl`) so the run itself never crosses the
    Windows/WSL2 filesystem boundary, and the results are copied back into
    *scenario_dir* afterwards. Requires ``pip install smartwatts`` inside
    *distro*.
    """
    wsl_work_dir, stage_error = stage_scenario_into_wsl(scenario_dir, distro)
    if wsl_work_dir is None:
        return ScenarioResult(
            load_level=load_level, run_dir=run_dir, scenario_dir=scenario_dir,
            status="failed", details=f"staging input into WSL2 failed: {stage_error}",
            duration_seconds=time.perf_counter() - start,
        )

    try:
        shell_cmd = build_smartwatts_wsl_shell_command(wsl_work_dir, python_exe)
        try:
            result = _run_wsl(distro, shell_cmd)
        except FileNotFoundError:
            return ScenarioResult(
                load_level=load_level, run_dir=run_dir, scenario_dir=scenario_dir,
                status="failed", details="wsl.exe not found in PATH",
                duration_seconds=time.perf_counter() - start,
            )
        except Exception as exc:  # pragma: no cover - defensive runtime safeguard
            return ScenarioResult(
                load_level=load_level, run_dir=run_dir, scenario_dir=scenario_dir,
                status="failed", details=str(exc),
                duration_seconds=time.perf_counter() - start,
            )

        if result.returncode != 0:
            details = (result.stderr or result.stdout or "SmartWatts processing failed").strip()
            return ScenarioResult(
                load_level=load_level, run_dir=run_dir, scenario_dir=scenario_dir,
                status="failed", details=details, duration_seconds=time.perf_counter() - start,
            )

        unstage_error = unstage_wsl_results(wsl_work_dir, scenario_dir, distro)
        wsl_work_dir = None  # already removed by unstage_wsl_results
        if unstage_error:
            return ScenarioResult(
                load_level=load_level, run_dir=run_dir, scenario_dir=scenario_dir,
                status="failed", details=f"copying results out of WSL2 staging dir failed: {unstage_error}",
                duration_seconds=time.perf_counter() - start,
            )

        duration = time.perf_counter() - start
        energy_reports = find_energy_consumption_reports(scenario_dir)
        if not energy_reports:
            return ScenarioResult(
                load_level=load_level, run_dir=run_dir, scenario_dir=scenario_dir,
                status="failed",
                details="SmartWatts command completed but no PowerReport.csv files were generated under energy-consumption.slice",
                duration_seconds=duration,
            )

        return ScenarioResult(
            load_level=load_level, run_dir=run_dir, scenario_dir=scenario_dir,
            status="recalculated", details=(result.stdout or "").strip(), duration_seconds=duration,
        )
    finally:
        if wsl_work_dir:
            _run_wsl(distro, f'rm -rf "{wsl_work_dir}"')


def recalculate_smartwatts_for_scenario(
    load_level: str,
    run_dir: Path,
    scenario_dir: Path,
    dry_run: bool = False,
    engine: str = "podman",
    wsl_distro: str | None = None,
    wsl_python: str = DEFAULT_WSL_PYTHON,
) -> ScenarioResult:
    """Delete and regenerate SmartWatts results for a single PowerAPI scenario.

    Assumes the caller has already decided this scenario needs (re)calculation
    (see :func:`classify_scenario`) -- any existing ``smartwattsresults``
    directory is unconditionally removed and rebuilt.

    *engine* selects how SmartWatts is run: ``"podman"`` (default) runs the
    containerized image via a bind mount; ``"wsl"`` stages the input into a
    native WSL2 directory and runs the ``smartwatts`` PyPI package directly
    inside WSL2 via ``wsl.exe`` (see module docstring for why).
    """
    smartwatts_dir = scenario_dir / "smartwattsresults"

    if dry_run:
        if engine == "wsl":
            cmd_desc = build_smartwatts_wsl_shell_command("<staged WSL2 work dir>", wsl_python)
        else:
            cmd_desc = " ".join(build_smartwatts_command(scenario_dir))
        removal_state = "would remove" if smartwatts_dir.exists() else "would create"
        return ScenarioResult(
            load_level=load_level,
            run_dir=run_dir,
            scenario_dir=scenario_dir,
            status="dry-run",
            details=f"{removal_state} {smartwatts_dir} and run via {engine}: {cmd_desc}",
        )

    start = time.perf_counter()
    if smartwatts_dir.exists():
        shutil.rmtree(smartwatts_dir)

    if engine == "wsl":
        return _recalculate_via_wsl(load_level, run_dir, scenario_dir, smartwatts_dir, start, wsl_distro, wsl_python)
    return _recalculate_via_podman(load_level, run_dir, scenario_dir, smartwatts_dir, start)


def print_overview(infos: list[ScenarioInfo]) -> None:
    """Print a per-state summary and a per-scenario table for *infos*."""
    counts: dict[str, int] = {}
    for info in infos:
        counts[info.state] = counts.get(info.state, 0) + 1

    print("\n=== PowerAPI scenario overview ===")
    for state in ("complete", "needs_calculation", "missing_input"):
        if state in counts:
            print(f"  {state:<18} {counts[state]:>4} scenario(s)")

    if not infos:
        return

    print()
    env_w = max(len(i.env_name) for i in infos)
    load_w = max(len(i.load_level) for i in infos)
    run_w = max(len(i.run_dir.name) for i in infos)
    scen_w = max(len(i.scenario_dir.name) for i in infos)
    state_w = max(len(i.state) for i in infos)
    for info in infos:
        print(
            f"  [{info.state:<{state_w}}] env={info.env_name:<{env_w}} "
            f"load={info.load_level:<{load_w}} run={info.run_dir.name:<{run_w}} "
            f"scenario={info.scenario_dir.name:<{scen_w}} -- {info.details}"
        )
    print()


def parse_args() -> argparse.Namespace:
    """Parse the command line options for this script."""
    parser = argparse.ArgumentParser(
        description=(
            "Show an overview of PowerAPI scenarios under EXPERIMENT_RESULTS "
            "and remove/regenerate smartwattsresults for the ones that need it."
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
        help="Rebuild smartwattsresults even for scenarios already marked complete.",
    )
    parser.add_argument(
        "--stop-on-error",
        action="store_true",
        help="Stop launching new scenarios after the first one fails to recalculate.",
    )
    parser.add_argument(
        "--environment",
        type=str,
        default=None,
        help="Filter to a specific environment name. When omitted, all environments are processed.",
    )
    parser.add_argument(
        "--max-workers",
        type=int,
        default=DEFAULT_MAX_WORKERS,
        help=f"Number of SmartWatts runs to process in parallel (default: {DEFAULT_MAX_WORKERS}).",
    )
    parser.add_argument(
        "--engine",
        choices=["podman", "wsl"],
        default="podman",
        help=(
            "How to run SmartWatts. 'podman' (default) runs the containerized "
            "powerapi/smartwatts-formula image. 'wsl' runs the 'smartwatts' PyPI "
            "package directly inside a WSL2 distro via wsl.exe (no container "
            "layer) -- requires 'pip install smartwatts' inside that distro "
            "first. Always runs serially (--max-workers is ignored) regardless "
            "of engine choice for 'wsl', since SmartWatts' actor IPC uses a "
            "fixed, non-per-process socket path under /tmp that concurrent "
            "invocations on the same distro would collide on."
        ),
    )
    parser.add_argument(
        "--wsl-distro",
        type=str,
        default=None,
        help="WSL2 distro to use with --engine wsl (default: your default registered distro).",
    )
    parser.add_argument(
        "--wsl-python",
        type=str,
        default=DEFAULT_WSL_PYTHON,
        help=f"Python executable to invoke inside WSL2 with --engine wsl (default: {DEFAULT_WSL_PYTHON}).",
    )
    return parser.parse_args()


def main() -> int:
    """Survey all PowerAPI scenarios and rebuild the ones needing it.

    Prints an overview of which scenarios already have complete SmartWatts
    results before executing anything, then recalculates the rest with the
    selected engine. Returns a process exit code: 0 on success, 1 if the
    engine is unavailable or any scenario failed.
    """
    args = parse_args()
    root = Path(__file__).resolve().parent

    if not args.dry_run:
        if args.engine == "podman":
            if not podman_is_available():
                print("podman executable not found in PATH.")
                return 1
        elif not wsl_smartwatts_is_available(args.wsl_distro, args.wsl_python):
            distro_note = f" distro '{args.wsl_distro}'" if args.wsl_distro else ""
            print(
                f"Could not import 'smartwatts'/'powerapi' via '{args.wsl_python}' "
                f"inside WSL2{distro_note}. Install it there first: pip install smartwatts"
            )
            return 1

    scenarios = list(iter_powerapi_scenarios(root, environment=args.environment))
    if not scenarios:
        print("No PowerAPI scenarios found under EXPERIMENT_RESULTS.")
        return 0

    infos = [
        ScenarioInfo(env_name, load_level, run_dir, scenario_dir, *classify_scenario(scenario_dir, args.force_remove))
        for env_name, load_level, run_dir, scenario_dir in scenarios
    ]
    print_overview(infos)

    to_process = [info for info in infos if info.state == "needs_calculation"]
    if not to_process:
        print("Nothing to (re)calculate.")
        return 0

    max_workers = max(1, min(args.max_workers, len(to_process)))
    if args.engine == "wsl" and max_workers > 1:
        print(
            "Note: --engine wsl forces serial execution (max-workers=1) -- see "
            "--help for why concurrent bare WSL2 invocations aren't safe.\n"
        )
        max_workers = 1
    print(f"Processing {len(to_process)} scenario(s) using up to {max_workers} parallel worker(s) (engine={args.engine})...\n")

    stop_event = threading.Event()

    def run_one(info: ScenarioInfo) -> ScenarioResult:
        """Recalculate one scenario, honouring the --stop-on-error flag.

        Executed from the thread pool. If an earlier scenario already failed
        and ``--stop-on-error`` is active, ``stop_event`` is set and this
        returns a ``skipped`` result without starting another SmartWatts run.
        """
        if stop_event.is_set():
            return ScenarioResult(
                load_level=info.load_level,
                run_dir=info.run_dir,
                scenario_dir=info.scenario_dir,
                status="skipped",
                details="stopped after an earlier failure (--stop-on-error)",
            )
        result = recalculate_smartwatts_for_scenario(
            load_level=info.load_level,
            run_dir=info.run_dir,
            scenario_dir=info.scenario_dir,
            dry_run=args.dry_run,
            engine=args.engine,
            wsl_distro=args.wsl_distro,
            wsl_python=args.wsl_python,
        )
        if result.status == "failed" and args.stop_on_error:
            stop_event.set()
        return result

    results: list[ScenarioResult] = []
    overall_start = time.perf_counter()

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(run_one, info): info for info in to_process}
        for future in as_completed(futures):
            result = future.result()
            results.append(result)
            print(
                f"load={result.load_level} run={result.run_dir.name} "
                f"scenario={result.scenario_dir.name} status={result.status} "
                f"({result.duration_seconds:.1f}s)"
            )
            if result.details:
                print(f"  {result.details}")

    wall_seconds = time.perf_counter() - overall_start
    cpu_seconds = sum(r.duration_seconds for r in results)

    recalculated = sum(1 for r in results if r.status == "recalculated")
    skipped = sum(1 for r in results if r.status == "skipped")
    failed = sum(1 for r in results if r.status == "failed")
    dry_run_count = sum(1 for r in results if r.status == "dry-run")

    print(
        "\nSummary: "
        f"processed={len(to_process)} recalculated={recalculated} dry_run={dry_run_count} "
        f"skipped={skipped} failed={failed} "
        f"already_complete={sum(1 for i in infos if i.state == 'complete')} "
        f"missing_input={sum(1 for i in infos if i.state == 'missing_input')}"
    )
    if not args.dry_run:
        print(
            f"Wall time: {wall_seconds:.1f}s "
            f"(cumulative processing time: {cpu_seconds:.1f}s across {max_workers} worker(s))"
        )

    if failed > 0:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())