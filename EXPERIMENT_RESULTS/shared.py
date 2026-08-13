"""
shared.py - Shared directory traversal for experiment-analysis scripts.

Directory layout::

    EXPERIMENT_RESULTS/
        Container/
            230/                 # run dirs
            230_run2/
            350/
            20250923_150700_spring_docker_tools/  # scenario subdirs
        VM/
            230/
            230_2/
            350/
            20260619_210721_spring_vm_scaphandre/  # scenario subdirs


Recommended pattern – analyse each environment (Container, VM, …)
separately so their data never mixes::

    from shared import discover_environments, build_run_dirs

    root = Path(__file__).resolve().parent
    for env_name in discover_environments(root):
        run_dirs = build_run_dirs(root, env_name)
        for load_level in sorted(run_dirs, key=int):  # "230", "350", …
            for run_dir in run_dirs[load_level]:
                for scenario_dir in run_dir.iterdir():
                    # scenario_dir is inside run_dir
                    …


If merging is truly desired, pass ``environment=None`` (default).
"""

from __future__ import annotations

import json
import os
import re
import zipfile
from pathlib import Path

import numpy as np
import pandas as pd
from collections import defaultdict


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _is_run_dir(name: str) -> bool:
    """Return True if *name* looks like a run directory (e.g. ``350``,
    ``350_run2``)."""
    return name.split("_")[0].isdigit()


def _extract_tool_suffix(scenario_name: str) -> str:
    """Extract the tool suffix from a scenario directory name.

    Handles both ``spring_docker_*`` and ``spring_vm_*`` naming patterns.
    Examples:
        ``20251011_152909_spring_docker_kepler`` -> ``kepler``
        ``20260619_210721_spring_vm_scaphandre`` -> ``scaphandre``
    """
    parts = scenario_name.split("_")
    for part in reversed(parts):
        if part in TOOLS or part in ("none", "idle", "tools"):
            return part
    # Fallback: last part
    return parts[-1] if parts else ""


# ---------------------------------------------------------------------------
# Power data loading / parsing (shared across most analysis scripts)
# ---------------------------------------------------------------------------

def trim_time_series(
    df: pd.DataFrame,
    trim_seconds: float = 0,
    jmeter_bounds: tuple | None = None,
) -> pd.DataFrame:
    """Remove records outside a time window.

    If *jmeter_bounds* ``(start, end)`` is provided (both not ``None``),
    only rows with ``df['datetime']`` in that range are returned.
    Otherwise a symmetric relative trim is applied: the first and last
    *trim_seconds* seconds of the series are discarded.
    """
    if jmeter_bounds is not None and all(jmeter_bounds):
        start, end = jmeter_bounds
        return df[(df["datetime"] >= start) & (df["datetime"] <= end)]
    if trim_seconds <= 0:
        return df
    start_time = df["datetime"].min()
    end_time = df["datetime"].max()
    cutoff_start = start_time + pd.Timedelta(seconds=trim_seconds)
    cutoff_end = end_time - pd.Timedelta(seconds=trim_seconds)
    return df[(df["datetime"] >= cutoff_start) & (df["datetime"] <= cutoff_end)]


def load_rittal_data(
    file_path: str,
    trim_seconds: float = 0,
    jmeter_bounds: tuple | None = None,
) -> pd.DataFrame:
    """Load Rittal CSV, sum power per timestamp, return ``datetime``+``Power (Watts)``."""
    df = pd.read_csv(file_path)
    df["Timestamp"] = pd.to_numeric(df["Timestamp"], errors="coerce")
    df = df.dropna(subset=["Timestamp"])
    power_data = df.groupby("Timestamp")["Power (Watts)"].sum().reset_index()
    power_data["datetime"] = pd.to_datetime(power_data["Timestamp"], unit="ms")
    if trim_seconds > 0 or jmeter_bounds is not None:
        power_data = trim_time_series(power_data, trim_seconds, jmeter_bounds=jmeter_bounds)
    return power_data


def calculate_power_from_energy(
    df: pd.DataFrame,
    trim_seconds: float = 0,
    jmeter_bounds: tuple | None = None,
) -> pd.DataFrame:
    """Calculate power (W) from energy (µJ) in powercap‑style CSV data.

    Parameters
    ----------
    df :
        DataFrame with columns ``Timestamp`` (epoch ms) and
        `` Energy (micro joules)`` and ``Domain``.
    verbose :
        When ``True``, prints diagnostics for every negative‑power sample.
    """
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
                _print_verbose_negative_power(domain, domain_data, energy_diff, time_diff, power, idx)
                power[idx] = np.nan
        domain_data["Power"] = power
        domain_data = domain_data.dropna(subset=["Power"])
        power_data.append(domain_data)
    result = pd.concat(power_data)
    domain_counts = result.groupby("datetime")["Domain"].count()
    complete_timestamps = domain_counts[domain_counts == len(df["Domain"].unique())].index
    result_filtered = result[result["datetime"].isin(complete_timestamps)]
    power_sum = result_filtered.groupby("datetime")["Power"].sum().reset_index()
    if trim_seconds > 0 or jmeter_bounds is not None:
        power_sum = trim_time_series(power_sum, trim_seconds, jmeter_bounds=jmeter_bounds)
    return power_sum


def load_power_data(
    file_path: str,
    trim_seconds: float = 0,
    jmeter_bounds: tuple | None = None,
) -> pd.DataFrame:
    """Dispatch to ``load_rittal_data`` or ``calculate_power_from_energy``."""
    if "rittal" in file_path.lower():
        return load_rittal_data(file_path, trim_seconds, jmeter_bounds=jmeter_bounds)
    elif "powercap" in file_path.lower():
        df = pd.read_csv(file_path)
        return calculate_power_from_energy(df, trim_seconds, jmeter_bounds=jmeter_bounds)
    else:
        raise ValueError(f"Unknown file type: {file_path}")


def get_jmeter_time_bounds(jmeter_dir, trim_seconds):
    """Find the jmeter .jtl file in *jmeter_dir*, read the ``timeStamp`` column,
    and return ``(trimmed_start, trimmed_end)`` as ``pandas.Timestamp``,
    or ``(None, None)`` if not found.
    """
    jtl_file = None
    for root, dirs, files in os.walk(jmeter_dir):
        for f in files:
            if f.endswith(".jtl"):
                jtl_file = os.path.join(root, f)
                break
        if jtl_file:
            break
    if not jtl_file:
        return None, None
    try:
        df = pd.read_csv(jtl_file, low_memory=False)
        if df.empty or "timeStamp" not in df.columns:
            return None, None
        min_ts = df["timeStamp"].min()
        max_ts = df["timeStamp"].max()
        min_time = pd.to_datetime(min_ts, unit="ms")
        max_time = pd.to_datetime(max_ts, unit="ms")
        trimmed_start = min_time + pd.Timedelta(seconds=trim_seconds)
        trimmed_end = max_time - pd.Timedelta(seconds=trim_seconds)
        return trimmed_start, trimmed_end
    except Exception as e:
        print(f"Error reading jmeter file {jtl_file}: {e}")
        return None, None


def extract_service_pids(experiment_log_path: str) -> list[str]:
    """Read *experiment_log_path* (``.jsonl``) and return ``service_pids`` as strings."""
    collected: list[str] = []
    try:
        with open(experiment_log_path, "r", encoding="utf-8") as f:
            for line in f:
                try:
                    entry = json.loads(line)
                    if "service_pids" in entry:
                        collected.extend([str(pid) for pid in entry["service_pids"]])
                except Exception:
                    continue
    except Exception as e:
        print(f"Error reading {experiment_log_path}: {e}")
    return collected


# ---------------------------------------------------------------------------
# Discover / build
# ---------------------------------------------------------------------------

def discover_environments(root: Path) -> list[str]:
    """Return a sorted list of environment subdirectory names
    (e.g. ``["Container", "VM"]``).

    An environment directory is one that:
    - is **not** named with a numeric prefix (so it is not a flat run dir), and
    - contains at least one run directory as a direct child.
    """
    envs: list[str] = []
    for child in sorted(root.iterdir()):
        if not child.is_dir():
            continue
        if _is_run_dir(child.name):
            continue
        if any(_is_run_dir(c.name) for c in child.iterdir() if c.is_dir()):
            envs.append(child.name)
    return envs


def build_run_dirs(root: Path, *, environment: str | None = None) -> dict[str, list[Path]]:
    """Return ``{load_level: [run_dir, …]}``.

    Each ``run_dir`` points to a concrete directory that holds scenario
    subdirectories (e.g. ``Container/350``, ``Container/230_run2``, or
    flat ``480``).  The *scenario* directories themselves are found by
    calling ``run_dir.iterdir()`` — this function does **not** look
    inside them.

    Parameters
    ----------
    root :
        Path to the ``EXPERIMENT_RESULTS`` directory (i.e. the script's
        parent directory).
    environment :
        Restrict to one environment (``"Container"``, ``"VM"``, …).
        When ``None`` (default), both flat dirs and nested ones are
        merged.  Pass a specific name to analyse that environment alone.

    Example – separate analysis::

        envs = discover_environments(root)   # ["Container", "VM"]
        for env in envs:
            runs = build_run_dirs(root, environment=env)
            for level in sorted(runs, key=int):
                for run_dir in runs[level]:
                    for scenario_dir in run_dir.iterdir():
                        # …

    Example – legacy merge (not recommended)::

        runs = build_run_dirs(root)
    """
    level_map: dict[str, list[Path]] = defaultdict(list)
    candidates: list[Path] = []

    if environment is not None:
        # Scoped: only inside the named environment subdir.
        env_dir = root / environment
        if env_dir.is_dir():
            candidates = [d for d in env_dir.iterdir() if d.is_dir()]
    else:
        # Full scan: flat + one level deep.
        for child in root.iterdir():
            if child.is_dir():
                candidates.append(child)
        for env_dir in root.iterdir():
            if env_dir.is_dir() and not _is_run_dir(env_dir.name):
                candidates.extend(
                    d for d in env_dir.iterdir() if d.is_dir()
                )

    for directory in candidates:
        prefix = directory.name.split("_")[0]
        if prefix.isdigit():
            level_map[prefix].append(directory)

    return {k: sorted(v) for k, v in level_map.items()}


def _print_verbose_negative_power(domain, domain_data, energy_diff, time_diff, power, idx):
    """Print verbose diagnostics for negative RAPL power (used by visualizeIdlePowerConsumptionAsBoxPlot)."""
    print(f"\nNegative power value found for Domain {domain}:")
    print(f"Timestamp: {domain_data.loc[idx, 'datetime']} {domain_data.loc[idx, 'Timestamp']}")
    print(f"Energy diff: {energy_diff[idx]} \u00b5J")
    print(f"Time diff: {time_diff[idx]} s")
    print(f"Calculated power: {power[idx]} W")
    print("\nInvolved records:")
    print(f"Current record:")
    print(domain_data.loc[idx, [" Energy (micro joules)", "datetime"]].to_string())
    print(f"Skipping negative power value for Domain {domain} as RAPL might have overflown")


# ---------------------------------------------------------------------------
# HTTP logger parsers (identical between Container and Process scripts)
# ---------------------------------------------------------------------------

def parse_scaphandre_http_logger(
    file_path: str,
    service_pids,
    trim_seconds: float = 0,
    jmeter_bounds: tuple | None = None,
    split_containers: bool = False,
) -> pd.DataFrame:
    """Parse Scaphandre http_logger CSV, return per-pid power series.

    Reads *file_path* in a single pass regardless of split_containers — the
    file can be 100+ MB, so re-scanning it once per container PID would
    double the I/O and regex work.
    """
    if split_containers and len(service_pids) != 2:
        raise ValueError(
            f"Expected exactly 2 service PIDs but got {len(service_pids)}"
        )
    pid_set = set(service_pids)
    data_line_re = re.compile(r"^DATA:.* at (\d+)")
    scaphandre_line_re = re.compile(
        r'scaph_process_power_consumption_microwatts\{[^}]*pid="(\d+)"[^}]*\} ([\d\.eE+-]+)'
    )
    records = []
    current_timestamp = None
    with open(file_path, "r", encoding="utf-8", errors="ignore") as fh:
        for line in fh:
            m = data_line_re.match(line)
            if m:
                current_timestamp = int(m.group(1))
                continue
            if current_timestamp is not None:
                sm = scaphandre_line_re.match(line)
                if sm:
                    pid, value = sm.group(1), sm.group(2)
                    if pid in pid_set:
                        records.append((pid, current_timestamp, float(value) / 1_000_000))

    def build_df(pids):
        rows = [
            {"datetime": pd.to_datetime(ts, unit="ms"), "Power": value}
            for pid, ts, value in records
            if pid in pids
        ]
        df = pd.DataFrame(rows, columns=["datetime", "Power"])
        if not df.empty and (trim_seconds > 0 or jmeter_bounds is not None):
            df = trim_time_series(df, trim_seconds, jmeter_bounds=jmeter_bounds)
        return df

    if split_containers:
        return [build_df({service_pids[0]}), build_df({service_pids[1]})]
    return build_df(pid_set)


# ---------------------------------------------------------------------------
# JoularJX result archive parsing
# ---------------------------------------------------------------------------

# Matches the per-second "all methods" power sample files JoularJX writes,
# e.g. ``joularJX-1-1783703386138-all-methods-power.csv``. Group 1 is the
# sample timestamp (epoch ms) embedded in the file name.
_JOULARJX_POWER_FILE_RE = re.compile(
    r"^joularJX-\d+-(\d+)-all-methods-power\.csv$"
)

# JoularJX's own top-level result directory name: ``<pid>-<start_ts>``, e.g.
# ``1-1783703300478``.
_JOULARJX_EXTRACTED_DIR_RE = re.compile(r"^\d+-\d+$")


def _ensure_joularjx_extracted(zip_path) -> Path | None:
    """Extract a ``joularjx-result*.zip`` archive next to itself, unless its
    top-level result directory (``<pid>-<start_ts>``) is already there.

    Repeated re-processing runs then skip the (comparatively expensive) zip
    decompression entirely and read the plain CSV files straight off disk.
    """
    zip_path = Path(zip_path)
    extract_dir = zip_path.parent
    with zipfile.ZipFile(zip_path) as zf:
        names = zf.namelist()
        if not names:
            return None
        top_level = names[0].split("/")[0]
        target = extract_dir / top_level
        if not target.is_dir():
            zf.extractall(extract_dir)
    return target if target.is_dir() else None


def _sum_joularjx_power_csv_by_group(file_path, group_fn) -> dict:
    """Sum (and count) the rows of one JoularJX per-second power-sample CSV
    (unheaded ``<class.method>,<power_watts>`` rows), grouped by
    ``group_fn(method_name)``.

    Rows for which ``group_fn`` returns ``None`` are skipped. Returns
    ``{group: (sum, count)}``.
    """
    totals: dict = defaultdict(lambda: [0.0, 0])
    with open(file_path, "r", encoding="utf-8", errors="ignore") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            method, _, value = line.rpartition(",")
            group = group_fn(method)
            if group is None:
                continue
            try:
                float_value = float(value)
            except ValueError:
                continue
            totals[group][0] += float_value
            totals[group][1] += 1
    return {group: (total, count) for group, (total, count) in totals.items()}


def _sum_joularjx_power_csv(file_path) -> float:
    """Sum the power column of one JoularJX per-second ``all-methods-power``
    CSV sample (unheaded ``<class.method>,<power_watts>`` rows)."""
    return _sum_joularjx_power_csv_by_group(file_path, lambda _method: "total").get("total", (0.0, 0))[0]


def _check_joularjx_energy_totals(result_dir, samples) -> None:
    """Sanity-check JoularJX's own numbers against each other.

    Each raw per-second ``all-methods-power`` sample is really the energy
    (in Joules) accumulated since the previous flush, whatever that
    interval's actual length — JoularJX just numerically reports it as
    "power" under a fixed-1s assumption (see :func:`_parse_joularjx_result_dir`).
    So summing the *raw, uncorrected* samples over the whole captured
    runtime (not trimmed to steady state) should closely match the
    cumulative total JoularJX itself reports in
    ``all/total/methods/joularJX-*-all-methods-energy.csv``. Prints both
    totals and their delta; does nothing if no total-energy file exists.
    """
    total_files = sorted(Path(result_dir).glob("all/total/methods/joularJX-*-all-methods-energy.csv"))
    if not total_files or not samples:
        return
    runtime_sum = sum(value for _, value in samples)
    total_sum = sum(_sum_joularjx_power_csv(f) for f in total_files)
    delta = runtime_sum - total_sum
    pct = (delta / total_sum * 100) if total_sum else float("nan")
    print(
        f"[{Path(result_dir).name}] JoularJX energy sanity check: "
        f"runtime samples sum={runtime_sum:.2f} J vs total file sum={total_sum:.2f} J "
        f"-> delta={delta:.2f} J ({pct:+.2f}%)"
    )


def _correct_joularjx_sample_rate(
    samples: list[tuple[int, float]],
    gap_threshold: float = 1.1,
) -> list[tuple[int, float]]:
    """Apply JoularJX's fixed-1s-power-assumption correction to a
    timestamp-sorted list of ``(timestamp_ms, raw_value)`` samples.

    JoularJX converts each sample's accumulated energy to power assuming a
    fixed 1-second sampling interval; under normal conditions its actual
    write cadence still jitters up to ~1.4s, so a plain ">1s" threshold
    would rescale nearly every sample. When its own sampling thread falls
    behind further (e.g. under heavy load) and a sample is only flushed
    several seconds after the previous one, the 1-second assumption
    properly breaks: the reported value is really the energy accumulated
    over the whole gap, reported as if it were a 1-second power reading,
    producing an extreme spike. To correct this without touching normal
    jitter, samples more than *gap_threshold* seconds after the previous one
    are divided by the actual elapsed time (in seconds) to recover the true
    average power over that interval; all other samples are passed through
    unchanged. Used by both the process-level (:func:`_parse_joularjx_result_dir`)
    and per-transaction-type (:func:`parse_joularjx_grouped_power`) parsers so
    they apply the exact same policy.
    """
    corrected = []
    previous_timestamp_ms = None
    for timestamp_ms, value in samples:
        power = value
        if previous_timestamp_ms is not None:
            elapsed_seconds = (timestamp_ms - previous_timestamp_ms) / 1000
            if elapsed_seconds > gap_threshold:
                power = value / elapsed_seconds
        corrected.append((timestamp_ms, power))
        previous_timestamp_ms = timestamp_ms
    return corrected


def _parse_joularjx_result_dir(
    result_dir,
    trim_seconds: float = 0,
    jmeter_bounds: tuple | None = None,
) -> pd.DataFrame:
    """Parse one extracted JoularJX result directory into a per-second
    process power series by summing all method power values reported in
    each ``all/runtime/methods/joularJX-*-all-methods-power.csv`` sample.

    See :func:`_correct_joularjx_sample_rate` for the per-sample correction
    applied to the raw values before they become ``Power``.
    """
    samples = []
    methods_dir = Path(result_dir) / "all" / "runtime" / "methods"
    for file_path in methods_dir.glob("joularJX-*-all-methods-power.csv"):
        m = _JOULARJX_POWER_FILE_RE.match(file_path.name)
        if not m:
            continue
        timestamp_ms = int(m.group(1))
        total_power = _sum_joularjx_power_csv(file_path)
        samples.append((timestamp_ms, total_power))
    samples.sort(key=lambda s: s[0])

    _check_joularjx_energy_totals(result_dir, samples)

    records = [
        {"datetime": pd.to_datetime(timestamp_ms, unit="ms"), "Power": power}
        for timestamp_ms, power in _correct_joularjx_sample_rate(samples)
    ]

    df = pd.DataFrame(records, columns=["datetime", "Power"])
    if not df.empty:
        df = df.sort_values("datetime").reset_index(drop=True)
        if trim_seconds > 0 or jmeter_bounds is not None:
            df = trim_time_series(df, trim_seconds, jmeter_bounds=jmeter_bounds)
    return df


def parse_joularjx_grouped_power(
    methods_dir,
    file_glob: str,
    group_fn,
    groups: list[str],
    trim_seconds: float = 0,
    jmeter_bounds: tuple | None = None,
) -> dict[str, pd.DataFrame]:
    """Parse a directory of JoularJX per-second power-sample CSVs into one
    per-second power series per group (e.g. per transaction type), sharing
    file discovery, the fixed-1s-power correction
    (:func:`_correct_joularjx_sample_rate`), and trimming with the
    process-level parser (:func:`_parse_joularjx_result_dir`).

    *file_glob* selects the sample files inside *methods_dir* (e.g.
    ``"joularJX-*-filtered-methods-power.csv"``); each file's timestamp is
    parsed from the digits before ``-methods-power.csv`` in its name (same
    convention as ``all-methods-power.csv`` files). *group_fn(method_name)*
    assigns each CSV row to one of *groups*; rows it maps to ``None`` are
    ignored. Every group gets one row per discovered file, defaulting to a
    zero-value sample when nothing in that file matched it, so the elapsed
    time between samples used for the correction is identical across
    groups — matching how JoularJX actually wrote the files (one flush per
    second covering all groups at once).

    Returns ``{group: DataFrame[['datetime', 'Power', 'Energy', 'Count']]}``.
    ``Power`` is the corrected per-second power; ``Energy`` is the raw,
    uncorrected sum (in Joules, JoularJX's native unit) — use it rather than
    ``Power`` when accumulating a total-energy figure; ``Count`` is the
    number of matching rows (e.g. invocations) in that sample.
    """
    methods_dir = Path(methods_dir)
    file_re = re.compile(r"^joularJX-\d+-(\d+)-.*\.csv$")
    file_samples = []
    for file_path in methods_dir.glob(file_glob):
        m = file_re.match(file_path.name)
        if not m:
            continue
        timestamp_ms = int(m.group(1))
        group_sums = _sum_joularjx_power_csv_by_group(file_path, group_fn)
        file_samples.append((timestamp_ms, group_sums))
    file_samples.sort(key=lambda s: s[0])

    result = {}
    for group in groups:
        raw_samples = [(ts, sums.get(group, (0.0, 0))[0]) for ts, sums in file_samples]
        counts = [sums.get(group, (0.0, 0))[1] for _, sums in file_samples]
        corrected = _correct_joularjx_sample_rate(raw_samples)
        records = [
            {
                "datetime": pd.to_datetime(ts, unit="ms"),
                "Power": power,
                "Energy": raw_value,
                "Count": count,
            }
            for (ts, power), (_, raw_value), count in zip(corrected, raw_samples, counts)
        ]
        df = pd.DataFrame(records, columns=["datetime", "Power", "Energy", "Count"])
        if not df.empty:
            df = df.sort_values("datetime").reset_index(drop=True)
            if trim_seconds > 0 or jmeter_bounds is not None:
                df = trim_time_series(df, trim_seconds, jmeter_bounds=jmeter_bounds)
        result[group] = df
    return result


def ensure_joularjx_unzipped(scenario_dir) -> None:
    """Extract every ``joularjx-result_*.zip`` found under *scenario_dir* into
    its parent directory (idempotent — see :func:`_ensure_joularjx_extracted`)."""
    for zip_path in Path(scenario_dir).glob("**/joularjx-result_*.zip"):
        _ensure_joularjx_extracted(zip_path)


def parse_joularjx_power(
    scenario_dir,
    trim_seconds: float = 0,
    jmeter_bounds: tuple | None = None,
    split_containers: bool = False,
) -> pd.DataFrame | list:
    """Parse process power directly from JoularJX's own ``all-methods-power``
    output, instead of estimating it from procfs CPU share and powercap.

    Looks for ``joularjx-result_*.zip`` (container 1 / single-container) and,
    when ``split_containers`` is True, also ``joularjx-result-2_*.zip``
    (container 2) inside *scenario_dir* — the same "no suffix" / ``-2``
    naming convention already used for PowerAPI slices in RS2/RS3. Each
    archive is extracted next to itself once (see
    :func:`_ensure_joularjx_extracted`) and then read from disk.

    Some older captures (e.g. RS1 Container/VM) never zip the JoularJX
    output at all — the ``<pid>-<start_ts>`` result directory already sits
    directly inside ``joularjx-result_*/`` on disk. When no archive is
    found, that already-extracted directory is used as-is.

    Returns a DataFrame with columns ``['datetime', 'Power']``, or a list of
    two such DataFrames (container 1, container 2) when split_containers.
    Missing archives/directories yield an empty DataFrame.
    """
    scenario_dir = Path(scenario_dir)
    empty = pd.DataFrame(columns=["datetime", "Power"])

    def find_result_dir(suffix: str):
        zip_candidates = sorted(scenario_dir.glob(f"**/joularjx-result{suffix}_*.zip"))
        if zip_candidates:
            return _ensure_joularjx_extracted(zip_candidates[0])
        for batch_dir in sorted(scenario_dir.glob(f"**/joularjx-result{suffix}_*")):
            if not batch_dir.is_dir():
                continue
            for child in sorted(batch_dir.iterdir()):
                if child.is_dir() and _JOULARJX_EXTRACTED_DIR_RE.match(child.name):
                    return child
        return None

    def load(suffix: str) -> pd.DataFrame:
        result_dir = find_result_dir(suffix)
        if result_dir is None:
            return empty
        return _parse_joularjx_result_dir(result_dir, trim_seconds, jmeter_bounds)

    if split_containers:
        return [load(""), load("-2")]
    return load("")


def filter_application_powerapi_files(file_paths: list) -> list:
    """Return only the application-container PowerReport.csv paths, discarding
    monitoring-tool containers such as the HWPC sensor.

    When SmartWatts is used, both the test-application container and the HWPC
    sensor container sit inside the same cgroup slice, so the file glob returns
    a CSV for each.  The monitoring container consumes far less power than the
    application under load.  Any container whose mean reported power is below
    5 % of the maximum mean power across all candidates is treated as a
    monitoring tool and excluded.

    A single file is always returned unchanged.
    """
    if len(file_paths) <= 1:
        return list(file_paths)

    mean_powers: dict[str, float] = {}
    for path in file_paths:
        try:
            df = pd.read_csv(str(path))
            if "power" in df.columns and not df.empty:
                mean_powers[str(path)] = float(df["power"].mean())
            else:
                mean_powers[str(path)] = 0.0
        except Exception:
            mean_powers[str(path)] = 0.0

    max_power = max(mean_powers.values()) if mean_powers else 0.0
    if max_power <= 0:
        return list(file_paths)

    threshold = 0.05 * max_power
    return [p for p in file_paths if mean_powers.get(str(p), 0.0) >= threshold]


def parse_powerapi_report(
    file_path: str,
    trim_seconds: float = 0,
    jmeter_bounds: tuple | None = None,
) -> pd.DataFrame:
    """Parse SmartWatts PowerReport CSV, aggregate to per-timestamp power."""
    df = pd.read_csv(file_path)
    if "timestamp" not in df.columns or "power" not in df.columns:
        return pd.DataFrame(columns=["datetime", "Power"])

    power_data = df[["timestamp", "power"]].copy()
    power_data["timestamp"] = pd.to_numeric(power_data["timestamp"], errors="coerce")
    power_data = power_data.dropna(subset=["timestamp"])
    power_data["datetime"] = pd.to_datetime(power_data["timestamp"], unit="ms")
    power_data = (
        power_data.groupby("datetime")["power"].sum().reset_index()
    )
    power_data = power_data.rename(columns={"power": "Power"})

    if trim_seconds > 0 or jmeter_bounds is not None:
        power_data = trim_time_series(
            power_data, trim_seconds, jmeter_bounds=jmeter_bounds
        )
    return power_data


def parse_powerapi_reports(
    file_paths: list,
    trim_seconds: float = 0,
    jmeter_bounds: tuple | None = None,
) -> pd.DataFrame:
    """Parse multiple SmartWatts PowerReport CSVs and return a single combined
    per-timestamp power DataFrame.

    Monitoring-tool containers (e.g. the HWPC sensor) are excluded via
    :func:`filter_application_powerapi_files` before aggregation.  If more
    than one application container remains their per-timestamp power values
    are summed.
    """
    filtered = filter_application_powerapi_files(file_paths)
    if not filtered:
        return pd.DataFrame(columns=["datetime", "Power"])

    dfs = [parse_powerapi_report(str(p), trim_seconds, jmeter_bounds) for p in filtered]
    dfs = [d for d in dfs if not d.empty]
    if not dfs:
        return pd.DataFrame(columns=["datetime", "Power"])
    if len(dfs) == 1:
        return dfs[0]

    combined = pd.concat(dfs)
    combined = combined.groupby("datetime")["Power"].sum().reset_index()
    return combined


# ---------------------------------------------------------------------------
# Scenario matching helpers (Container vs VM naming patterns)
# ---------------------------------------------------------------------------

TOOLS = frozenset(["tools", "kepler", "scaphandre", "otjae", "joularjx", "powerapi"])

_SCENARIO_SUFFIXES: dict[str, list[str]] = {
    "tools":      ["idle_no_tools", "idle", "none", "tools"],
    "kepler":     ["kepler"],
    "scaphandre": ["scaphandre"],
    "otjae":      ["otjae"],
    "joularjx":   ["joularjx"],
    "powerapi":   ["powerapi"],
    "idle":       ["idle_no_tools", "idle", "none"],
}


def strip_rs_suffix(scenario_dir_name: str) -> str:
    """Remove a trailing runtime-setup marker (``_rs2``, ``_rs3``, …) from a
    scenario directory name so the tool suffix becomes the last component
    again (e.g. ``…_spring_docker_kepler_rs2`` -> ``…_spring_docker_kepler``).
    """
    return re.sub(r"_rs\d+$", "", scenario_dir_name, flags=re.IGNORECASE)


def scenario_matches(scenario_dir_name: str, category: str) -> bool:
    """Return True if *scenario_dir_name* belongs to *category*.

    Handles both ``spring_docker_*`` and ``spring_vm_*`` naming patterns,
    including RS2/RS3 scenario names with a trailing ``_rs2``/``_rs3`` marker.
    Categories: tools, kepler, scaphandre, otjae, joularjx, powerapi, idle.
    """
    scenario_dir_name = strip_rs_suffix(scenario_dir_name)
    if category not in _SCENARIO_SUFFIXES:
        return False
    allowed = _SCENARIO_SUFFIXES[category]
    # Check exact suffix match
    if any(scenario_dir_name.endswith(sfx) for sfx in allowed):
        return True
    # Check spring_docker_* or spring_vm_* patterns
    for sfx in allowed:
        if scenario_dir_name.endswith(f"spring_docker_{sfx}"):
            return True
        if scenario_dir_name.endswith(f"spring_vm_{sfx}"):
            return True
        if scenario_dir_name.endswith(f"docker_{sfx}"):
            return True
        if scenario_dir_name.endswith(f"vm_{sfx}"):
            return True
    return False


Scenario = dict  # {load_level: dict[scenario_name, data]}


def scenario_matches_any(scenario_dir_name: str, suffixes: list[str]) -> bool:
    """Return True if *scenario_dir_name* matches any of the given suffix strings.

    Each suffix is a category name (e.g. ``"tools"``, ``"kepler"``) that is
    looked up via :func:`scenario_matches`.
    """
    return any(scenario_matches(scenario_dir_name, cat) for cat in suffixes)


def get_procfs_glob_pattern(scenario_dir_name: str) -> str:
    """Return the glob pattern for procfs files matching *scenario_dir_name*.

    Extracts the tool suffix and returns a pattern that matches both
    ``procfs_spring_docker_<tool>_*`` and ``procfs_spring_vm_<tool>_*``.
    """
    tool = _extract_tool_suffix(scenario_dir_name)
    return f"**/procfs_*_{tool}*.csv"


def get_http_logger_glob_pattern(scenario_dir_name: str) -> str:
    """Return the glob pattern for http_logger files matching *scenario_dir_name*.

    Extracts the tool suffix and returns a pattern that matches both
    ``http_logger_spring_docker_<tool>*`` and ``http_logger_spring_vm_<tool>*``.
    """
    tool = _extract_tool_suffix(scenario_dir_name)
    return f"**/http_logger_*_{tool}*.csv"


def docker_or_vm_endswith(scenario_dir_name: str, tool: str) -> bool:
    """Return True if the scenario name ends with a docker/VM variant of *tool*.

    Matches patterns like::
        docker_tools, spring_docker_tools, spring_vm_tools
        docker_scaphandre, spring_docker_scaphandre, spring_vm_scaphandre
        etc.
    """
    return (
        scenario_dir_name.endswith(f"docker_{tool}")
        or scenario_dir_name.endswith(f"spring_docker_{tool}")
        or scenario_dir_name.endswith(f"spring_vm_{tool}")
    )


# ---------------------------------------------------------------------------
# Repetition-level statistics (n=3 reps per condition: variability, effect
# size, and significance reporting for the paper's result tables)
# ---------------------------------------------------------------------------
#
# These helpers deliberately avoid a scipy dependency: at the small, fixed
# sample sizes this project actually has (n=3 repetitions; at most n=12
# session-paired observations for the Wilcoxon test), a hardcoded t-critical
# lookup and a full sign-enumeration Wilcoxon are both exact and trivial to
# compute, so pulling in scipy just for this would be unjustified.

import math
from itertools import product as _product

_T_CRITICAL_95_TWO_SIDED: dict[int, float] = {
    1: 12.706204736174698, 2: 4.302652729911275, 3: 3.182446305283708,
    4: 2.7764451051977987, 5: 2.5705818356363126, 6: 2.4469118487915749,
    7: 2.3646242515928956, 8: 2.3060041350333704, 9: 2.2621571627409915,
    10: 2.2281388519649385,
    11: 2.201, 12: 2.179, 13: 2.160, 14: 2.145, 15: 2.131,
    16: 2.120, 17: 2.110, 18: 2.101, 19: 2.093, 20: 2.086,
    25: 2.060, 30: 2.042,
}


def t_critical_95(df: int) -> float | None:
    """Two-sided 95% critical t-value for *df* degrees of freedom.

    Hardcoded lookup table (covers df=1..20, plus 25/30, i.e. up to n=21
    repetitions); linearly interpolated for df values between table
    entries, and falls back to the standard-normal critical value (1.960)
    for df > 30, since the t-distribution converges to it there. Returns
    ``None`` only if df <= 0 (undefined, e.g. a single repetition).
    """
    if df is None or df <= 0:
        return None
    if df in _T_CRITICAL_95_TWO_SIDED:
        return _T_CRITICAL_95_TWO_SIDED[df]
    if df > 30:
        return 1.960
    keys = sorted(_T_CRITICAL_95_TWO_SIDED.keys())
    for i in range(len(keys) - 1):
        if keys[i] < df < keys[i + 1]:
            x0, x1 = keys[i], keys[i + 1]
            y0, y1 = _T_CRITICAL_95_TWO_SIDED[x0], _T_CRITICAL_95_TWO_SIDED[x1]
            return y0 + (y1 - y0) * (df - x0) / (x1 - x0)
    return 1.960


def summarize_repetitions(values: list[float]) -> dict:
    """Summarize a list of *per-repetition* means for one condition.

    ``values`` should already be one scalar per repetition (e.g. one mean
    per run directory), not pooled raw per-second samples. Returns
    ``{"n", "mean", "std", "ci_lo", "ci_hi", "ci_margin"}``; ``std``/CI
    fields are ``None`` when n<2 (cannot estimate spread from a single
    observation) rather than raising.
    """
    vals = [float(v) for v in values if v is not None and not (isinstance(v, float) and math.isnan(v))]
    n = len(vals)
    if n == 0:
        return {"n": 0, "mean": None, "std": None, "ci_lo": None, "ci_hi": None, "ci_margin": None}
    mean = float(np.mean(vals))
    if n < 2:
        return {"n": n, "mean": mean, "std": None, "ci_lo": None, "ci_hi": None, "ci_margin": None}
    std = float(np.std(vals, ddof=1))
    t_crit = t_critical_95(n - 1)
    if t_crit is None:
        return {"n": n, "mean": mean, "std": std, "ci_lo": None, "ci_hi": None, "ci_margin": None}
    margin = t_crit * std / math.sqrt(n)
    return {"n": n, "mean": mean, "std": std, "ci_lo": mean - margin, "ci_hi": mean + margin, "ci_margin": margin}


def cohens_d_paired(diffs: list[float]) -> float | None:
    """Paired Cohen's dz = mean(diffs) / std(diffs) (ddof=1).

    ``diffs`` should be matched-pair differences (e.g. session-matched
    Tool A - Tool B). Returns ``None`` if n<2 or the differences have zero
    variance (dz is undefined, not infinite, in that degenerate case).
    """
    vals = [float(v) for v in diffs if v is not None]
    n = len(vals)
    if n < 2:
        return None
    std = float(np.std(vals, ddof=1))
    if std == 0:
        return None
    return float(np.mean(vals)) / std


def cohens_d_independent(sample_a: list[float], sample_b: list[float]) -> float | None:
    """Independent-samples Cohen's d using the pooled standard deviation.

    Fallback for comparisons where session-level pairing isn't available
    (e.g. Container vs. VM, which are different physical setups/sessions).
    Returns ``None`` if either sample has n<2 or the pooled std is zero.
    """
    a = [float(v) for v in sample_a if v is not None]
    b = [float(v) for v in sample_b if v is not None]
    n_a, n_b = len(a), len(b)
    if n_a < 2 or n_b < 2:
        return None
    mean_a, mean_b = float(np.mean(a)), float(np.mean(b))
    std_a, std_b = float(np.std(a, ddof=1)), float(np.std(b, ddof=1))
    pooled_var = ((n_a - 1) * std_a ** 2 + (n_b - 1) * std_b ** 2) / (n_a + n_b - 2)
    pooled_std = math.sqrt(pooled_var)
    if pooled_std == 0:
        return None
    return (mean_a - mean_b) / pooled_std


def _std_normal_cdf(x: float) -> float:
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def wilcoxon_signed_rank_exact(diffs: list[float]) -> dict:
    """Exact two-sided Wilcoxon signed-rank test via full sign enumeration.

    Appropriate at the small n this project uses (e.g. n=12 session-paired
    observations, 4 load levels x 3 repetitions) where scipy is neither
    available nor necessary — full enumeration (2^n sign assignments) is
    exact and fast up to n~20; a normal approximation is used above that
    as a defensive fallback, though it is not expected to be hit here.

    Zero differences are dropped first, per the standard Wilcoxon
    convention (documented via the returned ``n`` vs. ``n_nonzero``).
    Returns ``{"n", "n_nonzero", "statistic", "p_two_sided", "p_floor",
    "method"}``; ``p_floor`` is the mathematically smallest achievable
    two-sided p-value at this n (2 / 2^n_nonzero), included so a
    non-significant result at very small n is visibly a power limitation
    rather than a silent surprise.
    """
    n = len(diffs) if diffs else 0
    nonzero = [float(d) for d in diffs if d is not None and d != 0]
    n_nonzero = len(nonzero)
    if n_nonzero < 1:
        return {"n": n, "n_nonzero": n_nonzero, "statistic": None, "p_two_sided": None, "p_floor": None, "method": None}

    abs_vals = np.abs(nonzero)
    order = np.argsort(abs_vals, kind="mergesort")
    sorted_abs = abs_vals[order]
    ranks = np.empty(n_nonzero)
    i = 0
    while i < n_nonzero:
        j = i
        while j < n_nonzero and sorted_abs[j] == sorted_abs[i]:
            j += 1
        avg_rank = (i + 1 + j) / 2.0
        for k in range(i, j):
            ranks[order[k]] = avg_rank
        i = j

    signs_observed = np.sign(nonzero)
    w_plus_observed = float(np.sum(ranks[signs_observed > 0]))

    if n_nonzero > 20:
        mean_w = n_nonzero * (n_nonzero + 1) / 4.0
        var_w = n_nonzero * (n_nonzero + 1) * (2 * n_nonzero + 1) / 24.0
        z = (w_plus_observed - mean_w) / math.sqrt(var_w) if var_w > 0 else 0.0
        p_two_sided = min(1.0, 2 * (1 - _std_normal_cdf(abs(z))))
        return {"n": n, "n_nonzero": n_nonzero, "statistic": w_plus_observed, "p_two_sided": p_two_sided, "p_floor": None, "method": "normal-approx"}

    all_w_plus = np.array([
        sum(r for r, s in zip(ranks, signs) if s > 0)
        for signs in _product((1, -1), repeat=n_nonzero)
    ])
    total = len(all_w_plus)
    le = float(np.sum(all_w_plus <= w_plus_observed)) / total
    ge = float(np.sum(all_w_plus >= w_plus_observed)) / total
    p_two_sided = min(1.0, 2 * min(le, ge))
    p_floor = 2.0 / (2 ** n_nonzero)
    return {"n": n, "n_nonzero": n_nonzero, "statistic": w_plus_observed, "p_two_sided": p_two_sided, "p_floor": p_floor, "method": "exact"}


def mann_whitney_u_exact(sample_a: list[float], sample_b: list[float]) -> dict:
    """Two-sided Mann-Whitney U test.

    Unpaired analog of ``wilcoxon_signed_rank_exact``, used for comparisons
    where the two samples cannot be treated as matched pairs (e.g. a tool
    run and the baseline P_S run were collected as separate test setups,
    not necessarily in the same experimental session). Exact under the null
    distribution of rank sums via a subset-sum DP (equivalent to the method
    scipy/R use for small samples) when the pooled sample is tie-free;
    the DP assumes distinct integer ranks 1..n, so it is not valid once ties
    are present. In that case, and for combined sample sizes beyond what
    plain enumeration would be fast for, a normal approximation is used
    instead, with the standard tie-correction term in the variance when
    ties are present (this project's sample sizes, n up to ~15 per group,
    never reach the large-n branch, but real repetition means could tie).

    Returns ``{"n_a", "n_b", "statistic", "p_two_sided", "p_floor",
    "method"}``, where ``statistic`` is the Mann-Whitney U statistic for
    sample_a (U = W - n_a(n_a+1)/2, where W is the rank-sum of sample_a used
    internally) and ``p_floor`` is the smallest attainable two-sided p-value
    at this n_a/n_b (``None`` when the normal approximation is used, since
    that p-value is not drawn from a discrete floor-bounded distribution).
    """
    a = [float(v) for v in sample_a if v is not None]
    b = [float(v) for v in sample_b if v is not None]
    n_a, n_b = len(a), len(b)
    if n_a < 1 or n_b < 1:
        return {"n_a": n_a, "n_b": n_b, "statistic": None, "p_two_sided": None, "p_floor": None, "method": None}

    combined = np.array(a + b)
    order = np.argsort(combined, kind="mergesort")
    sorted_vals = combined[order]
    n = n_a + n_b
    ranks = np.empty(n)
    tie_correction = 0.0
    has_ties = False
    i = 0
    while i < n:
        j = i
        while j < n and sorted_vals[j] == sorted_vals[i]:
            j += 1
        avg_rank = (i + 1 + j) / 2.0
        for k in range(i, j):
            ranks[order[k]] = avg_rank
        t = j - i
        if t > 1:
            has_ties = True
            tie_correction += t ** 3 - t
        i = j
    w_a_observed = float(np.sum(ranks[:n_a]))
    u_a_observed = w_a_observed - n_a * (n_a + 1) / 2.0

    if has_ties or n > 60:
        mean_w = n_a * (n + 1) / 2.0
        if has_ties:
            var_w = (n_a * n_b / 12.0) * ((n + 1) - tie_correction / (n * (n - 1)))
            method = "normal-approx-tie-corrected"
        else:
            var_w = n_a * n_b * (n + 1) / 12.0
            method = "normal-approx"
        z = (w_a_observed - mean_w) / math.sqrt(var_w) if var_w > 0 else 0.0
        p_two_sided = min(1.0, 2 * (1 - _std_normal_cdf(abs(z))))
        return {"n_a": n_a, "n_b": n_b, "statistic": u_a_observed, "p_two_sided": p_two_sided, "p_floor": None, "method": method}

    min_w = n_a * (n_a + 1) // 2
    max_w = min_w + n_a * n_b
    dp = [[0] * (max_w + 1) for _ in range(n_a + 1)]
    dp[0][0] = 1
    for value in range(1, n + 1):
        for k in range(min(n_a, value), 0, -1):
            prev = dp[k - 1]
            row = dp[k]
            for s in range(max_w, value - 1, -1):
                if prev[s - value]:
                    row[s] += prev[s - value]
    counts = dp[n_a]
    total = float(sum(counts))
    w_int = int(round(w_a_observed))
    le = float(sum(counts[:w_int + 1])) / total if w_int >= 0 else 0.0
    ge = float(sum(counts[w_int:])) / total if w_int <= max_w else 0.0
    p_two_sided = min(1.0, 2 * min(le, ge))
    p_floor = 2.0 / total
    return {"n_a": n_a, "n_b": n_b, "statistic": u_a_observed, "p_two_sided": p_two_sided, "p_floor": p_floor, "method": "exact"}


def per_run_means(scenario_dict: dict, scenario_key: str, subkey: str) -> dict:
    """Group ``(run_label, series)`` tuples stored under scenario keys ending
    with *scenario_key* by run label, returning ``{run_label: per-run mean}``.

    Expects ``scenario_dict[some_key][subkey]`` to be a list of
    ``(run_label, pandas.Series)`` tuples (the run-label-tagged collection
    format used by the visualize*.py scripts). Collapses raw per-second
    samples to one scalar per repetition (run directory) *before* any
    cross-repetition averaging, so repetition-level variability survives for
    downstream :func:`summarize_repetitions` calls.
    """
    per_run: dict = defaultdict(list)
    for k in scenario_dict:
        if k.endswith(scenario_key):
            for run_label, series in scenario_dict[k].get(subkey, []):
                per_run[run_label].append(series)
    return {
        run_label: float(pd.concat(series_list, ignore_index=True).mean())
        for run_label, series_list in per_run.items()
        if series_list
    }


def fmt_mean_std(mean: float | None, std: float | None = None, unit: str = "W", decimals: int = 2) -> str:
    """Format a mean +/- std value for a LaTeX table cell, e.g. ``"56.56$\\pm$2.13W"``.

    Falls back to a bare mean (no ``std``) if the standard deviation is
    unavailable (n<2), or ``"-"`` if the mean itself is ``None``.
    """
    if mean is None:
        return "-"
    if std is None:
        return f"{mean:.{decimals}f}{unit}"
    return f"{mean:.{decimals}f}$\\pm${std:.{decimals}f}{unit}"
