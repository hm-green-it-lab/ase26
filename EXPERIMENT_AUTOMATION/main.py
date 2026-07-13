# main.py
"""
CLI entrypoint to run a reader-flow experiment.

Usage
-----
python -m main --config path/to/config.yaml
"""

from __future__ import annotations

import re

import argparse
import sys
from pathlib import Path

import yaml
from dotenv import load_dotenv

from orchestrator.runner import run_experiment
from helper.hooks import (
    start_docker,
    stop_docker,
    start_vm_docker,
    stop_vm_docker,
)
from helper.vm import (
    prepare_vm,
    sync_files,
    sync_files_to_host,
    mount_share,
    validate_pipeline,
    cleanup_vm_docker,
    cleanup_joularjx_vm,
)
from helper.jar_downloader import ensure_local_jars, ensure_remote_jars_local
#from vm_bootstrap import bootstrap_vm

# Load environment from .env early (SSH/JMeter creds, etc.)
load_dotenv()
# Also load paths.env so that SUT_BASE_DIR and related path variables
# are available in os.environ at runtime (not only for YAML substitution).
load_dotenv(dotenv_path=Path(__file__).resolve().parent / "paths.env", override=False)

# Centralize known experiment families to keep dispatch compact.
BASELINE_EXPERIMENT_TYPES = {
    "baseline_idle_no_tools",
    "baseline_idle",
}

DOCKER_EXPERIMENT_TYPES = {
    "spring_docker_none",
    "spring_docker_idle",
    "spring_docker_tools",
    "spring_docker_kepler",
    "spring_docker_scaphandre",
    "spring_docker_otjae",
    "spring_docker_joularjx",
    "spring_docker_powerapi",
    "spring_docker_scaphandre_rs2",
    "spring_docker_joularjx_rs2",
    "spring_docker_otjae_rs2",
    "spring_docker_kepler_rs2",
    "spring_docker_powerapi_rs2",
    "spring_docker_scaphandre_rs3",
    "spring_docker_joularjx_rs3",
    "spring_docker_otjae_rs3",
    "spring_docker_kepler_rs3",
    "spring_docker_powerapi_rs3",
}

VM_EXPERIMENT_TYPES = {
    "spring_vm_kepler",
    "spring_vm_scaphandre",
    "spring_vm_powerapi",
    "spring_vm_joularjx",
    "spring_vm_otjae",
    "spring_vm_tools",
}

TOOL_MAPPING = {
    "spring_vm_scaphandre": "scaphandre",
    "spring_vm_joularjx": "joularjx",
    "spring_vm_otjae": "otajae",
    "spring_vm_tools": "tools",
}


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse CLI arguments."""
    parser = argparse.ArgumentParser(description="Run reader-flow experiment.")
    parser.add_argument("--config", required=True, help="Path to YAML config file")
    parser.add_argument(
        "--total-rate",
        type=int,
        default=None,
        help="Override jmeter.props.total_rate from config",
    )
    parser.add_argument(
        "--container-2-load",
        type=int,
        default=None,
        help="Override jmeter.props.container_2_load from config",
    )
    return parser.parse_args(argv)


def _load_yaml(path: Path) -> dict:
    # Load env vars from central file
    env_path = Path(__file__).resolve().parent / "paths.env"
    env_values = load_env_file(env_path)

    # Read raw YAML text
    with path.open("r", encoding="utf-8") as f:
        raw = f.read()

    # Replace ${VAR}
    resolved = substitute_env_vars(raw, env_values)

    return yaml.safe_load(resolved)
    

def load_env_file(path: Path) -> dict:
    values = {}
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                k, v = line.split("=", 1)
                values[k.strip()] = v.strip()
    return values


def substitute_env_vars(text: str, env: dict) -> str:
    pattern = re.compile(r"\$\{(\w+)\}")

    def replace(match):
        key = match.group(1)
        return env.get(key, match.group(0))  # fallback: leave unchanged

    return pattern.sub(replace, text)


def merge_configs(base_config: dict, extension_config: dict) -> dict:
    """Merge base and extension config dictionaries recursively."""
    merged = base_config.copy()

    def deep_merge(source: dict, destination: dict) -> dict:
        for key, value in source.items():
            if key in destination and isinstance(destination[key], dict) and isinstance(value, dict):
                deep_merge(value, destination[key])
            else:
                destination[key] = value
        return destination

    return deep_merge(extension_config, merged)


def _resolve_extends_path(base_ref: str, config_path: Path) -> Path:
    """Resolve `extends` paths with backward-compatible search locations."""
    raw_path = Path(base_ref)
    if raw_path.is_absolute():
        return raw_path.resolve()

    candidates = [
        (config_path.parent / raw_path).resolve(),
        (Path.cwd() / raw_path).resolve(),
        (Path(__file__).resolve().parent / raw_path).resolve(),
    ]

    for candidate in candidates:
        if candidate.exists():
            return candidate

    return candidates[0]


def load_config(path: str, _visited: set[Path] | None = None) -> dict:
    """Load YAML config from disk with optional recursive `extends` support."""
    config_path = Path(path).resolve()

    if _visited is None:
        _visited = set()
    if config_path in _visited:
        raise ValueError(f"Circular config inheritance detected at: {config_path}")
    _visited.add(config_path)

    config = _load_yaml(config_path)
    if config is None:
        config = {}
    if not isinstance(config, dict):
        raise ValueError(f"Config file must contain a mapping at top level: {config_path}")

    base_ref = config.pop("extends", None)
    if base_ref:
        if not isinstance(base_ref, str):
            raise ValueError("The `extends` value must be a path string.")

        base_path = _resolve_extends_path(base_ref, config_path)
        if not base_path.exists():
            raise FileNotFoundError(
                f"Base config referenced by `extends` was not found: '{base_ref}' "
                f"(from {config_path})"
            )

        base_config = load_config(str(base_path), _visited=_visited)
        return merge_configs(base_config, config)

    return config


def _run_with_optional_docker_hooks(config: dict, experiment_type: str) -> None:
    """Run experiment with Docker hooks only for Docker-backed experiment types."""
    if experiment_type in DOCKER_EXPERIMENT_TYPES:
        run_experiment(
            config,
            experiment_type=experiment_type,
            before_experiment_hook=start_docker,
            after_experiment_hook=stop_docker,
        )
        return

    run_experiment(config, experiment_type=experiment_type)


def _run_with_vm_hooks(config: dict, experiment_type: str) -> None:
    """Run experiment with VM hooks: tool on host, application on VM guest."""
    # Minimal bootstrap: for specific VM experiment types call the helper
    #if experiment_type in {
    #    "spring_vm_joularjx",
    #    "spring_vm_scaphandre",
    #    "spring_vm_otjae",
    #}:
    #    bootstrap_vm()

    run_experiment(
        config,
        experiment_type=experiment_type,
        before_experiment_hook=start_vm_docker,
        after_experiment_hook=stop_vm_docker,
    )

def _run_vm_experiment(config: dict, experiment_type: str) -> None:
    """
    VM experiment orchestration.

    Current implementation only provides the orchestration skeleton.
    Individual preparation steps are introduced incrementally.
    """

    measurement_tool = TOOL_MAPPING[experiment_type]

    prepare_vm(config, measurement_tool)

    sync_files(config)

    if measurement_tool == "joularjx":
        cleanup_joularjx_vm(config)

    mount_share(config, measurement_tool)

    cleanup_vm_docker(config, measurement_tool)

    validate_pipeline(config)

    run_experiment(
        config,
        experiment_type=experiment_type,
        before_experiment_hook=start_vm_docker,
        after_experiment_hook=stop_vm_docker,
    )

def dispatch(config: dict) -> None:
    """
    Dispatch to the appropriate experiment flow based on config['experiment']['type'].
    """
    experiment_type = config["experiment"]["type"]

    # 1) Download all required JARs first (local + remote), so they exist
    #    before any upload step.
    ensure_local_jars(config)
    ensure_remote_jars_local(config)

    # 2) Now sync files to the SUT host (clean state + upload docker files
    #    and the freshly-downloaded reader JARs to remote_dir).
    sync_files_to_host(config)

    if experiment_type in BASELINE_EXPERIMENT_TYPES or experiment_type in DOCKER_EXPERIMENT_TYPES:
        _run_with_optional_docker_hooks(config, experiment_type)
        return

    if experiment_type in VM_EXPERIMENT_TYPES:
        #_run_with_vm_hooks(config, experiment_type)
        _run_vm_experiment(config, experiment_type)
        return

    raise ValueError(f"Unknown experiment type: {experiment_type}")


def apply_cli_overrides(config: dict, args: argparse.Namespace) -> dict:
    """Apply runtime overrides from CLI flags without mutating YAML files."""
    if args.total_rate is not None:
        config.setdefault("jmeter", {}).setdefault("props", {})["total_rate"] = args.total_rate
        print(f"[CLI] Overriding jmeter.props.total_rate={args.total_rate}")

    if args.container_2_load is not None:
        config.setdefault("jmeter", {}).setdefault("props", {})[
            "container_2_load"
        ] = args.container_2_load
        print(
            f"[CLI] Overriding jmeter.props.container_2_load={args.container_2_load}"
        )
    return config


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    config = load_config(args.config)
    config = apply_cli_overrides(config, args)
    dispatch(config)


if __name__ == "__main__":
    main(sys.argv[1:])
