# jar_downloader.py
"""
Ensure that locally-required JAR files exist before an experiment starts.

If a JAR is missing and a corresponding `*_jar_url` key is present in the
experiment config, the file is downloaded automatically.  This avoids the
silent Java error
    "Error: Unable to access jarfile <path>"
that previously only appeared inside a CSV output file.

Supported config keys (all under experiment:)
---------------------------------------------
    rittal_jar_path     / rittal_jar_url
    http_logger_jar_path / http_logger_jar_url

Usage
-----
    from helper.jar_downloader import ensure_local_jars
    ensure_local_jars(config)
"""

from __future__ import annotations

import os
import urllib.request
from pathlib import Path

# Root of the EXPERIMENT_AUTOMATION directory – used to resolve relative paths
# in the config regardless of the current working directory.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# Pairs of (path_key, url_key) that should be checked before every run.
# ---------------------------------------------------------------------------
_LOCAL_JAR_PAIRS = [
    ("rittal_jar_path",      "rittal_jar_url"),
    ("http_logger_jar_path", "http_logger_jar_url"),
]


def ensure_local_jars(config: dict) -> None:
    """
    Verify that all locally-required JAR files exist.

    For each JAR whose path is configured in ``config['experiment']``:
    - If the file already exists → nothing to do.
    - If the file is missing and a ``*_jar_url`` is configured →
      download the file to the configured path.
    - If the file is missing and no URL is configured →
      raise ``FileNotFoundError`` with an actionable error message.

    Parameters
    ----------
    config : dict
        Full experiment configuration dictionary.

    Raises
    ------
    FileNotFoundError
        When a JAR is absent and no download URL is configured.
    RuntimeError
        When a download fails.
    """
    exp = config.get("experiment", {})

    for path_key, url_key in _LOCAL_JAR_PAIRS:
        raw_path = exp.get(path_key)
        if not raw_path:
            continue  # JAR not required by this experiment

        # Normalise path separators (handles Windows-style \ on Linux/Mac)
        # and resolve relative paths against the project root so the script
        # works correctly regardless of the current working directory.
        raw_normalised = os.path.normpath(raw_path)
        jar_path = Path(raw_normalised)
        if not jar_path.is_absolute():
            jar_path = (_PROJECT_ROOT / jar_path).resolve()

        if jar_path.is_file():
            print(f"[JAR] ✓ {path_key}: {jar_path}")
            continue

        # File is missing – try to download it
        url = (exp.get(url_key) or "").strip()
        if not url:
            raise FileNotFoundError(
                f"Required JAR not found and no download URL configured.\n"
                f"  config key : experiment.{path_key}\n"
                f"  path       : {jar_path}\n"
                f"  Fix        : add 'experiment.{url_key}: <download-url>' "
                f"to your config or place the JAR at the path above."
            )

        print(f"[JAR] '{jar_path.name}' not found – downloading from {url} ...")
        _download(url, jar_path)
        print(f"[JAR] ✓ Downloaded → {jar_path}")


# ---------------------------------------------------------------------------
# Pairs of (filename_key, url_key) for JARs that run *remotely* on the SUT.
# These are downloaded to the local tools/ directory so sync_files_to_host
# can upload them to remote_dir on the SUT before each experiment.
# ---------------------------------------------------------------------------
_REMOTE_JAR_PAIRS = [
    ("procfs_jar_filename",   "procfs_jar_url"),
    ("powercap_jar_filename", "powercap_jar_url"),
]


def ensure_remote_jars_local(config: dict) -> None:
    """
    Ensure that JARs which run remotely on the SUT exist in the local tools/
    directory so they can be uploaded to the SUT before each experiment.

    For each remote JAR whose filename is configured in ``config['experiment']``:
    - If the file already exists locally → nothing to do.
    - If the file is missing and a ``*_jar_url`` is configured → download it.
    - If the file is missing and no URL is configured → raise FileNotFoundError.

    Parameters
    ----------
    config : dict
        Full experiment configuration dictionary.

    Raises
    ------
    FileNotFoundError
        When a JAR is absent and no download URL is configured.
    RuntimeError
        When a download fails.
    """
    exp = config.get("experiment", {})
    local_tools = _PROJECT_ROOT / "tools"
    local_tools.mkdir(parents=True, exist_ok=True)

    for filename_key, url_key in _REMOTE_JAR_PAIRS:
        jar_name = exp.get(filename_key)
        if not jar_name:
            continue

        jar_path = local_tools / jar_name

        if jar_path.is_file():
            print(f"[JAR] ✓ {filename_key}: {jar_path}")
            continue

        url = (exp.get(url_key) or "").strip()
        if not url:
            raise FileNotFoundError(
                f"Required remote JAR not found locally and no download URL configured.\n"
                f"  config key : experiment.{filename_key}\n"
                f"  local path : {jar_path}\n"
                f"  Fix        : add 'experiment.{url_key}: <download-url>' "
                f"to your config or place the JAR at the path above."
            )

        print(f"[JAR] '{jar_name}' not found locally – downloading from {url} ...")
        _download(url, jar_path)
        print(f"[JAR] ✓ Downloaded → {jar_path}")


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _download(url: str, dest: Path) -> None:
    """Download *url* to *dest*, creating parent directories as needed."""
    dest.parent.mkdir(parents=True, exist_ok=True)

    tmp = dest.with_suffix(dest.suffix + ".download")
    try:
        urllib.request.urlretrieve(url, str(tmp), reporthook=_progress_hook)
        print()  # newline after progress output
        tmp.rename(dest)
    except Exception as exc:
        # Clean up incomplete download
        if tmp.exists():
            tmp.unlink()
        raise RuntimeError(
            f"Failed to download JAR from '{url}': {exc}"
        ) from exc


def _progress_hook(block_num: int, block_size: int, total_size: int) -> None:
    """Simple progress indicator printed to stdout."""
    if total_size > 0:
        downloaded = min(block_num * block_size, total_size)
        pct = downloaded * 100 // total_size
        bar_len = 30
        filled = bar_len * downloaded // total_size
        bar = "█" * filled + "░" * (bar_len - filled)
        print(f"\r    [{bar}] {pct:3d}%  ({downloaded:,} / {total_size:,} bytes)",
              end="", flush=True)




