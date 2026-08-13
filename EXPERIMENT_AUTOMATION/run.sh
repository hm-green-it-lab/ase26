#!/bin/bash

set -e

SCRIPT_PATH="$(dirname "$0")/main.py"
CONFIG_DIR="$(dirname "$0")/configuration"

# Find python executable
if ! command -v python3 &> /dev/null; then
    echo "Python executable not found in PATH. Activate your venv or install Python."
    exit 1
fi

PYTHON_CMD=$(command -v python3)

WAIT_BETWEEN_RUNS_SECONDS=300
REPETITIONS_PER_LOAD=3
LOAD_LEVELS=(350 480 560)
declare -A CONTAINER_2_LOADS
CONTAINER_2_LOADS[350]="50"
CONTAINER_2_LOADS[480]="67 33"
CONTAINER_2_LOADS[560]="50"

# Configs that use JMeter and should run for every load level.
LOAD_SWEEP_CONFIGS=("spring_docker_scaphandre_rs2.yml" "spring_docker_joularjx_rs2.yml" "spring_docker_otjae_rs2.yml" "spring_docker_kepler_rs2.yml" "spring_docker_powerapi_rs2.yml")

for config_file in "${LOAD_SWEEP_CONFIGS[@]}"; do
    config_path="${CONFIG_DIR}/${config_file}"

    for load_level in "${LOAD_LEVELS[@]}"; do
        for container_2_load in ${CONTAINER_2_LOADS[$load_level]}; do
            for rep in $(seq 1 "$REPETITIONS_PER_LOAD"); do
                echo ""
                echo "Running ${config_file} | load=${load_level} | container2_load=${container_2_load}% | repetition=${rep}/${REPETITIONS_PER_LOAD}"
                echo ""

                "${PYTHON_CMD}" "${SCRIPT_PATH}" --config "${config_path}" --total-rate "${load_level}" --container-2-load "${container_2_load}"
                if [ $? -ne 0 ]; then
                    echo "Experiment failed for config '${config_file}' (load=${load_level} container2_load=${container_2_load} rep=${rep})"
                    exit 1
                fi

                echo ""
                echo "Waiting 5 minutes before next run..."
                echo ""
                sleep "$WAIT_BETWEEN_RUNS_SECONDS"
            done # System Under Test (SUT) SSH Configuration
        done
    done
done