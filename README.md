# Evaluating attribution models and tools for software energy consumption at virtual machine, container, process, and transaction levels – Replication Package

This repository contains the data, scripts, and configuration files required to replicate the experiments from the paper. The structure and contents are aligned with the experiments and analyses described in the paper.

## Folder Structure

Key directories and their contents:

- [`EXPERIMENT_AUTOMATION/`](./EXPERIMENT_AUTOMATION/) – Automation scripts and configurations for running the experiments.
  - [`configuration/`](./EXPERIMENT_AUTOMATION/configuration/) – YAML configuration files for different experiment setups.
  - [`docker/`](./EXPERIMENT_AUTOMATION/docker/) – Docker Compose files and tool-specific configurations (server-side).
  - [`helper/`](./EXPERIMENT_AUTOMATION/helper/) – Python based helper scripts for measurements.
  - [`orchestrator/`](./EXPERIMENT_AUTOMATION/orchestrator/) – Python modules for controlling and evaluating measurements.
  - [`setup/`](./EXPERIMENT_AUTOMATION/setup/) – Setup automation for the SUT and the JMeter load driver (see its dedicated [README](./EXPERIMENT_AUTOMATION/setup/01_README.md)); also contains a bundled copy of the JMeter test plan ([`jmeter_testplan.jmx`](./EXPERIMENT_AUTOMATION/setup/jmeter_testplan.jmx)).
  - [`vms/`](./EXPERIMENT_AUTOMATION/vms/) – Shell scripts to start and control the QEMU/KVM guest VM used in the `spring_vm_*` experiments.
  - `output/` – Output files for the experiment runs (created at runtime).
  - [`main.py`](./EXPERIMENT_AUTOMATION/main.py) – Entry point for running a single experiment configuration.
  - [`.env-template`](./EXPERIMENT_AUTOMATION/.env-template) – Python .env template.
  - [`paths.env`](./EXPERIMENT_AUTOMATION/paths.env) – Hostnames, directories, tool download URLs, and path placeholders used by YAML configs.
  - [`run.ps1`](./EXPERIMENT_AUTOMATION/run.ps1) – Dedicated Windows PowerShell helper to execute multiple experiment configurations.
  - [`run.sh`](./EXPERIMENT_AUTOMATION/run.sh) – Linux/bash equivalent of `run.ps1`.
  - [`VM_setup.md`](./EXPERIMENT_AUTOMATION/VM_setup.md) – Instructions for creating the QEMU/KVM VM disk image used in the VM experiments.
  - [`README.md`](./EXPERIMENT_AUTOMATION/README.md) – Dedicated README for experiment automation with Python.

- [`EXPERIMENT_RESULTS/`](./EXPERIMENT_RESULTS/) – Experiment raw results including Python scripts for analysis and visualization of measurement results, e.g., [`visualizeLoadLevelContainerPowerConsumptionAsBoxplots.py`](./EXPERIMENT_RESULTS/visualizeLoadLevelContainerPowerConsumptionAsBoxplots.py), [`visualizeIdlePowerConsumptionAsBoxPlot.py`](./EXPERIMENT_RESULTS/visualizeIdlePowerConsumptionAsBoxPlot.py), etc.

The two parts of the package have separate dependency lists, since running the experiments and analyzing the results are independent activities:

- [`EXPERIMENT_AUTOMATION/requirements.txt`](./EXPERIMENT_AUTOMATION/requirements.txt) – Dependencies for the experiment automation (`python-dotenv`, `pyyaml`, `paramiko`).
- [`EXPERIMENT_RESULTS/requirements.txt`](./EXPERIMENT_RESULTS/requirements.txt) – Dependencies for the analysis and visualization scripts (`pandas`, `numpy`, `matplotlib`, `seaborn`).

If you only want to reproduce the figures and tables from the shipped raw data, you just need the second one:

```bash
pip install -r EXPERIMENT_RESULTS/requirements.txt
```

## Measurement Tools

In addition to the Python analysis scripts, the following measurement tools were used to collect energy and performance data during the experiments:

| Tool                                                                     | Description                                                                                                                                                                                                       |
|--------------------------------------------------------------------------|-------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| [PowercapReader](https://github.com/hm-green-it-lab/powercap-reader)     | Java-based tool that continuously reads RAPL data via powercap on Linux. Used to collect energy consumption measurements on the system under test (SUT).                                                          |
| [ProcFSReader](https://github.com/hm-green-it-lab/procfs-reader)         | Java-based tool that continuously reads resource demand data (CPU, memory, I/O, network) for processes from the Linux proc file system. Used to collect process- and system-level performance metrics on the SUT. |
| [RittalReader](https://github.com/hm-green-it-lab/rittal-reader)         | Java-based tool for reading power data from Rittal PDU devices via SNMP. Used for external power measurements from your local environment.                                                                        |
| [HTTPLogger](https://github.com/hm-green-it-lab/http-logger)             | Java-based tool for fetching metrics from Prometheus /metrics endpoints. Used for fetching the data from Kepler and Scaphandre.                                                                                   |
| [JMeter](https://jmeter.apache.org/)                                     | Load testing tool used to generate HTTP requests to the Spring REST application at controlled rates for each experiment scenario from the JMeter load driver.                                                     |
| [Kepler](https://github.com/sustainable-computing-io/kepler)             | Kepler is a Prometheus exporter that measures energy consumption metrics at the container and process level.                                                                                                      |
| [Scaphandre](https://github.com/hubblo-org/scaphandre)                   | Scaphandre is an agent for exposing server power and energy consumption metrics.                                                                                                                                  |
| [PowerAPI](https://github.com/powerapi-ng)                   | PowerAPI is a Software-defined power monitoring framework for estimating and attributing power consumption to containers and processes based on the SmartWatts formula.                                                                                                                                 |
| [JoularJX](https://github.com/joular/joularjx)                           | Java agent for measuring energy consumption of JVM-based applications at the process, thread, and method level.                                                                                                   |
| [OTJAE](https://github.com/RETIT/opentelemetry-javaagent-extension)      | OpenTelemetry Java-Agent Extension for attributing energy consumption to Java processes and transactions.                                                                                                         |
| [lm-sensors](https://github.com/lm-sensors/lm-sensors) | The lm-sensors package is used to measure the temperature of the CPU sockets before and after each test.                                                                                                          |

These tools were orchestrated and synchronized using the automation scripts described below to ensure reproducible and accurate measurements across all experiment runs. Before presenting the automation in detail, the next section explains the experiment initialization and setup required to prepare the servers and measurement environment.

## Environment Setup

To run the experiments using the experiment automation scripts (see [`EXPERIMENT_AUTOMATION/README.md`](./EXPERIMENT_AUTOMATION/README.md)), you need to prepare the system under test (SUT), the JMeter load driver, and your local environment. The following sections describe the required setup for each of these components.

### Automation Configuration Files (`.env` and `paths.env`)

Before running `python main.py --config ...` in [`EXPERIMENT_AUTOMATION/`](./EXPERIMENT_AUTOMATION/), adjust both environment files:

- `.env`
  - Create this file from [`.env-template`](./EXPERIMENT_AUTOMATION/.env-template)
  - Set SSH credentials:
    - `SUT_SSH_USER`, `SUT_SSH_PASSWORD`
    - `JMETER_SSH_USER`, `JMETER_SSH_PASSWORD`
    - `VM_SSH_USER`, `VM_SSH_PASSWORD` (only for `spring_vm_*` configs)
- [`paths.env`](./EXPERIMENT_AUTOMATION/paths.env)
  - Set host and base directory placeholders used by YAML config files (`${...}`), e.g.:
    - `SUT_HOST`, `SUT_BASE_DIR`
    - `JMETER_HOST`, `JMETER_BASE_DIR`
    - `LOCAL_BASE_DIR`
    - `VM_HOST`, `VM_PORT`, `VM_BASE_DIR` (only for `spring_vm_*` configs; `VM_HOST`/`VM_PORT` address the guest through the QEMU SSH port forward configured in [`vms/`](./EXPERIMENT_AUTOMATION/vms/), by default `127.0.0.1:2222`)
  - Set tool URLs used for automatic download when jars are missing:
    - `RITTAL_JAR_URL`, `HTTP_LOGGER_JAR_URL`
    - `PROCFS_JAR_URL`, `POWERCAP_JAR_URL`
  - Set Rittal SNMP connection values:
    - `RITTAL_SNMP_ADDRESS`, `RITTAL_SNMP_COMMUNITY`, `RITTAL_SNMP_OIDS`

Note that the `*_rs2.yml` configuration files (e.g., [`spring_docker_kepler_rs2.yml`](./EXPERIMENT_AUTOMATION/configuration/spring_docker_kepler_rs2.yml)) contain hardcoded NUMA/cpuset bindings for the multi-container load distribution experiments (e.g., `docker update --cpuset-cpus="0-19,40-59" --cpuset-mems="0,1"`). These values are tailored to the CPU topology of the system used in the paper and likely need to be adjusted to match the core/NUMA-node layout of your own SUT.

### System Under Test (SUT) Setup

On the SUT, install Docker and ensure the configured base directories are writable by the SSH user.

Furthermore, the automation executes several commands on the SUT via SSH that require root privileges (e.g., for mounting/unmounting, starting Docker containers, or controlling processes). To avoid interactive password prompts during automated runs, grant the SSH user passwordless sudo rights for these commands by adding the following entry to `/etc/sudoers` (e.g., via `sudo visudo`) on the SUT, replacing `user` with the configured SSH user:

```
user ALL=(ALL) NOPASSWD: /usr/bin/java, /bin/sh, /bin/kill, /usr/bin/chown, /usr/bin/pkill, /sbin/umount, /sbin/mount, /bin/mkdir, /bin/bash, /usr/bin/systemd-run, /usr/bin/qemu-system-x86_64, /usr/bin/docker, /var/scaphandre
```

You do **not** need to manually copy files from [`./EXPERIMENT_AUTOMATION/docker/`](./EXPERIMENT_AUTOMATION/docker/) to the SUT before each run. During each experiment run, the automation:

- cleans `${SUT_BASE_DIR}` on the SUT,
- uploads `EXPERIMENT_AUTOMATION/docker/` to `${SUT_BASE_DIR}/spring-rest-service`,
- uploads `EXPERIMENT_AUTOMATION/vms/` to `${SUT_BASE_DIR}/vm`.

Note that the scripts in [`EXPERIMENT_AUTOMATION/vms/`](./EXPERIMENT_AUTOMATION/vms/) (e.g., [`start_vm1.sh`](./EXPERIMENT_AUTOMATION/vms/start_vm1.sh)) only start and control an already existing VM — for the `spring_vm_*` configs you therefore need to manually create the VM disk image beforehand (see [`VM_setup.md`](./EXPERIMENT_AUTOMATION/VM_setup.md) for step-by-step instructions) at the path referenced in the scripts (e.g., `/home/user/ubuntu_disk.img`) and configure the same passwordless-sudo entry as on the SUT (see above) for the SSH user inside that VM as well.

> [!WARNING]
> The guest-side base directory is currently hardcoded to `/home/userv` in `sync_files()` in [`helper/vm.py`](./EXPERIMENT_AUTOMATION/helper/vm.py) and is *not* taken from `VM_BASE_DIR` in `paths.env`. If your guest VM uses a different user or home directory, adjust it in both places.

The docker command paths in the YAML files (for `remote_docker_start`, `remote_docker_stop`, and `remote_docker_logs`) are already aligned with this layout. The following is an example of the resulting folder structure on the SUT:

```
/home/user/spring-rest-service
├── docker-compose.override.joularjx.yaml
├── docker-compose.override.kepler.yaml
├── docker-compose.override.otel.yaml
├── docker-compose.override.scaphandre.yaml
├── docker-compose.yaml
├── docker-compose_2.yaml
├── joularjx
│   ├── config.properties
│   ├── config.vm.properties
│   ├── joularjx-3.0.1.jar
│   ├── joularjx-result
│   ├── results
│   └── zip
└── otel
    ├── otel_version
    └── otjae_version
```

- `docker-compose.yaml` is the base compose file describing the Spring REST application (container `C1`). `docker-compose_2.yaml` describes the second, co-located application container (`C2`) and is only used by the `*_rs2.yml`/`*_rs3.yml` load-distribution configs.
- The `docker-compose.override.*.yaml` files provide tool-specific overrides. Each override file enables and configures one measurement tooling stack. The naming convention is:
  - plain name (e.g. `…scaphandre.yaml`) – Container environment, single application container;
  - `…-vm.yaml` – the guest-side stack for the `spring_vm_*` configs, running inside the QEMU/KVM VM;
  - `…-host-vm.yaml` – the additional host-side instance used in the VM environment, which attributes power to the `qemu-system-x86_64` process and shares it with the guest;
  - `…_2.yaml` / `…-2.yaml` – the counterpart stack for the second container (`C2`) in the RS2/RS3 load-distribution experiments.
- The `joularjx/` directory contains the JoularJX agent jar and its configuration/results directories. `config.properties` is used in the Container environment and `config.vm.properties` inside the guest VM (see the [JoularJX README](./EXPERIMENT_AUTOMATION/docker/joularjx/README.md) for why a patched 3.0.1 build is bundled).
- The `otel/` directory pins the OpenTelemetry Java agent and OTJAE extension versions used for the TS6 measurements.

The full mapping of override files to experiment configurations is:

| Override file | Used by | Purpose |
| --- | --- | --- |
| `docker-compose.override.scaphandre.yaml` | `spring_docker_scaphandre*.yml` | Scaphandre on the host (TS2). |
| `docker-compose.override.scaphandre-vm.yaml` | `spring_vm_scaphandre.yml` | Scaphandre inside the guest VM. |
| `docker-compose.override.scaphandre-host-vm.yaml` | `spring_vm_scaphandre.yml` | Host-side Scaphandre QEMU exporter attributing power to the VM process. |
| `docker-compose.override.kepler.yaml` | `spring_docker_kepler*.yml` | Kepler (TS3, Container environment only). |
| `docker-compose.override.powerapi.yaml` | `spring_docker_powerapi*.yml` | PowerAPI HWPC sensor (TS4). |
| `docker-compose.override.joularjx.yaml` | `spring_docker_joularjx*.yml` | JoularJX agent on `C1` (TS5). |
| `docker-compose.override.joularjx_2.yaml` | `spring_docker_joularjx_rs2/rs3.yml` | JoularJX agent on `C2`. |
| `docker-compose.override.joularjx-vm.yaml` | `spring_vm_joularjx.yml` | JoularJX inside the guest VM. |
| `docker-compose.override.powerjoular-host-vm.yaml` | `spring_vm_joularjx.yml` | Host-side PowerJoular attributing power to the VM process. |
| `docker-compose.override.otel.yaml` | `spring_docker_otjae*.yml`, `spring_vm_otjae.yml` | OTJAE / OpenTelemetry agent on `C1` (TS6). |
| `docker-compose.override.otel-2.yaml` | `spring_docker_otjae_rs2/rs3.yml` | OTJAE / OpenTelemetry agent on `C2`. |

#### Spring REST Service Container Build

For using the Spring REST service docker container referenced in the docker-compose files, you need to build the container called `spring-rest-service:feature` on the SUT (see https://github.com/RETIT/opentelemetry-javaagent-extension/tree/main/examples/spring-rest-service). The following steps assume that you have git, docker, and Java (JDK 21+) installed on the machine used to build the artifact.

```bash
# clone the OTJAE repository
git clone https://github.com/RETIT/opentelemetry-javaagent-extension.git
cd opentelemetry-javaagent-extension

# In the paper, we have used v0.0.17-alpha, but in case you want to use a different version, you can checkout the corresponding tag
git checkout tags/v0.0.17-alpha

# On Linux, you need to make the mvnw script executable (only required in v0.0.17-alpha and earlier)
chmod +x ./mvnw

# Build the project and package the extension - skip tests for speed if you prefer
# Requires JAVA_HOME to be set to the JDK installation path
./mvnw -DskipTests package

# After a successful build you can check if the docker container is built correctly
docker images | grep spring-rest-service
# You can also test the container locally as follows
docker run spring-rest-service:feature
```

#### Further tool installation instructions

In addition to Docker, install the following tools on the SUT:

- [PowercapReader](https://github.com/hm-green-it-lab/powercap-reader)
  - Manual installation on the SUT is not required if `powercap_jar_url` is configured.
  - At runtime, the automation downloads missing jars locally and uploads them to `remote_dir` on the SUT.
  - Example config keys:
    - `remote_dir`: /home/user/work
    - `powercap_jar_filename`: powercap-reader-1.0-runner.jar
    - `powercap_jar_url`: https://...
- [ProcFSReader](https://github.com/hm-green-it-lab/procfs-reader)
    - Manual installation on the SUT is not required if `procfs_jar_url` is configured.
    - At runtime, the automation downloads missing jars locally and uploads them to `remote_dir` on the SUT.
    - Example config keys:
        - `remote_dir`: /home/user/work
        - `procfs_jar_filename`: procfs-reader-1.0-runner.jar
        - `procfs_jar_url`: https://...
- [lm-sensors](https://github.com/lm-sensors/lm-sensors) 
  - The automation scripts use lm-sensors to measure the temperature of the CPU sockets before and after each test. You need to install lm-sensors on the SUT.
      
### JMeter Load Driver Setup

On the JMeter load driver you should download Apache JMeter (https://jmeter.apache.org/) and extract it to a directory of your choice. In our experiments, we have used apache-jmeter-5.6.3 and placed it in a directory called /home/jmeter/apache-jmeter-5.6.3. It is important that you configure the `bin_path` property in your configuration scripts to point to the correct JMeter binary. Furthermore, you need to configure the location where the Jmeter script should store the result files in the `remote_dir` attribute of the `jmeter` configuration. The following is an example configuration for the JMeter load driver:

- `remote_dir`: /home/jmeter/output/         # <— used for .jtl and .log (timestamped)
- `bin_path`: /home/jmeter/apache-jmeter-5.6.3/bin/jmeter.sh

The Jmeter load test script for the experiments is included in this repository at [`EXPERIMENT_AUTOMATION/setup/jmeter_testplan.jmx`](./EXPERIMENT_AUTOMATION/setup/jmeter_testplan.jmx) (originally from https://github.com/RETIT/opentelemetry-javaagent-extension/blob/v0.0.18-alpha/examples/spring-rest-service/src/test/resources/jmeter_testplan.jmx) and can be placed on the JMeter load driver in the same directory as the JMeter binary. It is important to ensure that the load test script location is correctly specified in the `test_plan` attribute of the [`jmeter`](./EXPERIMENT_AUTOMATION/configuration/spring_docker_jmeter.yml) configuration:

- `test_plan`: /home/jmeter/jmeter_testplan.jmx

The following is an example folder structure on the JMeter load driver:

```
/home/jmeter
├── apache-jmeter-5.6.3
│   ├── bin
│   ├── lib
│   └── ...
└── jmeter_testplan.jmx
```

### Local Environment Setup

The following tools need to be installed on your local environment:

- Python (3.11+)
- Java (JDK 21+)
- Docker (20.10.14+)
- [RittalReader](https://github.com/hm-green-it-lab/rittal-reader)
    - If the file in `rittal_jar_path` is missing and `rittal_jar_url` is configured, the automation downloads it automatically before each run.
    - Example config keys:
        - `rittal_jar_path`: ./tools/rittal-reader-1.0-runner.jar
        - `rittal_jar_url`: https://...
    - The RittalReader is used to fetch the external power measurements from Rittal PDU devices via SNMP. The Rittal PDU connection details can be configured in the application.properties of the tool.
- [HTTPLogger](https://github.com/hm-green-it-lab/http-logger)
    - If the file in `http_logger_jar_path` is missing and `http_logger_jar_url` is configured, the automation downloads it automatically before each run.
    - Example config keys:
        - `http_logger_jar_path`: ./tools/http-logger-1.0-runner.jar
        - `http_logger_jar_url`: https://...
    - The HTTPLogger is used to fetch the data from Prometheus /metrics endpoints. The metrics endpoint can be configured using the `http_logger_url` property in the experiment configuration YAML files.
      - `http_logger_url`: http://127.0.0.1:28282/metrics

## Experiment Automation 

The experiment automation scripts are intended to be run on your local environment. For details on running experiments, see the [`EXPERIMENT_AUTOMATION/README.md`](./EXPERIMENT_AUTOMATION/README.md) which describes usage, configuration, and automation scripts in depth.

## Experiment Results

This [folder](./EXPERIMENT_RESULTS/) contains the raw measurement data and analysis scripts for the experiments presented in our paper.

The experiments were conducted using a Spring REST application deployed in Docker, with energy and performance measurements taken under varying load levels. All measurement data and results are organized by environment/runtime setup, load level, and timestamped experiment run, as described in the following subsections. The Python scripts in the [`EXPERIMENT_RESULTS/`](./EXPERIMENT_RESULTS/) folder process the raw data and generate the figures and tables used in the paper.

> [!IMPORTANT]
> The raw data is stored as `.zip` archives, one per load level. They have to be extracted before any analysis script will find data — see [Step 1: Extract the raw measurement archives](#step-1-extract-the-raw-measurement-archives-required) below.

### Directory Structure: Environments and Runtime Setups

The raw data is organized in four top-level directories that map to the environments and runtime setups (RS) described in the paper:

| **Directory** | **Environment (paper)** | **Runtime setup (paper)** | **Description** |
| --- | --- | --- | --- |
| [`Container/`](./EXPERIMENT_RESULTS/Container/) | Container | RS1 – full resources | Single application container directly on the host OS with access to all resources. |
| [`VM/`](./EXPERIMENT_RESULTS/VM/) | VM | RS1 – full resources | Single application container inside a QEMU/KVM guest VM. |
| [`RS2/`](./EXPERIMENT_RESULTS/RS2/) | Container | RS2 – dedicated resources | Two co-located application containers, each pinned to one CPU socket and its NUMA region. |
| [`RS3/`](./EXPERIMENT_RESULTS/RS3/) | Container | RS3 – shared resources | Two co-located application containers sharing the full hardware. |

Within `Container/` and `VM/`, the results are grouped by load level, and each load level was repeated three times. The repetition folders and their corresponding `.zip` archives are named `<load>` for the first repetition and `<load>_run2`/`<load>_run3` (in `Container/`, `RS2/`, and `RS3/`) or `<load>_2`/`<load>_3` (in `VM/`) for the second and third repetition. Each `.zip` archive contains all measurement data and logs for a single experiment run.

In `RS2/` and `RS3/`, a single fixed total load of 350 RPS per endpoint (i.e., 1050 T/s in the paper's notation) is distributed across the two containers C1 and C2 using three splits (50/50, 67/33, and 80/20). The folder names encode the split, e.g., `350_rs2_c1_67_c2_33_run2` = RS2, 67%/33% split between C1 and C2, repetition 2.

The additional archive [`VM/vm_scaphandre_6s_measurement_intervals.zip`](./EXPERIMENT_RESULTS/VM/vm_scaphandre_6s_measurement_intervals.zip) contains the extra Scaphandre VM runs with an increased 6-second measurement interval discussed in the container-level results section of the paper; these runs are not part of the regular three repetitions and must **not** be extracted into `VM/` (see the extraction step below).

### Test Setups: Scenario Folder Names

Each repetition folder contains one timestamped scenario folder per test setup (TS) of the paper, named `{YYYYMMDD}_{HHMMSS}_{configuration}`. The configuration names map to the paper's test setups as follows:

| **Configuration name** | **Test setup (paper)** | **Description** |
| --- | --- | --- |
| `baseline_idle_no_tools` | – | Empty system, external (Rittal) measurements only ("Idle" in the paper). Only present at load level 0. |
| `spring_docker_none` | – | Application container idle without measurement tooling ("TS1/RS1 no measurements" in the paper). Only present at load level 0. |
| `spring_docker_tools` / `spring_vm_tools` | TS1 | Baseline: application container plus the ProcFS/Powercap (RAPL)/Rittal measurement readers, without any attribution tool. |
| `spring_docker_scaphandre` / `spring_vm_scaphandre` | TS2 | Scaphandre attribution at container and process levels (plus VM level in the VM environment). |
| `spring_docker_kepler` | TS3 | Kepler attribution at container and process levels (Container environment only, as the evaluated Kepler version does not support VMs). |
| `spring_docker_powerapi` | TS4 | PowerAPI HWPC sensor recording; power attribution is computed offline with the SmartWatts formula (Container environment only). |
| `spring_docker_joularjx` / `spring_vm_joularjx` | TS5 | JoularJX attribution at process and transaction levels (using PowerJoular on the host in the VM environment). |
| `spring_docker_otjae` / `spring_vm_otjae` | TS6 | OTJAE resource-demand collection for model-based attribution at process and transaction levels. |

The `RS2/` and `RS3/` runs use the same configuration names with an `_rs2`/`_rs3` suffix (e.g., `spring_docker_kepler_rs2`) and contain only the five tool setups (TS2–TS6); no TS1 baseline was recorded for the load-distribution experiments.

Each configuration name above corresponds to the YAML file of the same name in [`EXPERIMENT_AUTOMATION/configuration/`](./EXPERIMENT_AUTOMATION/configuration/). That folder contains three further YAML files that do not appear as scenario folder names, because the other configurations inherit from them through the `extends:` key on their first line:

| **Base configuration** | **Extended by** | **Contains** |
| --- | --- | --- |
| [`spring_docker_jmeter.yml`](./EXPERIMENT_AUTOMATION/configuration/spring_docker_jmeter.yml) | all `spring_docker_*` configs | Common reader/JMeter/output settings for the Container environment. |
| [`spring_vm_jmeter.yml`](./EXPERIMENT_AUTOMATION/configuration/spring_vm_jmeter.yml) | all `spring_vm_*` configs | Same, for the VM environment (adds the guest VM lifecycle settings). |
| [`baseline_idle.yml`](./EXPERIMENT_AUTOMATION/configuration/baseline_idle.yml) | [`baseline_idle_no_tools.yml`](./EXPERIMENT_AUTOMATION/configuration/baseline_idle_no_tools.yml) | Idle measurement with the ProcFS/Powercap/Rittal readers. It is runnable on its own, but only its `_no_tools` variant — external Rittal measurements only — was used for the runs shipped here. |

### Load Levels

For each experiment run in `Container/` and `VM/`, the system was subjected to one of the following load intensities: **0**, **230**, **350**, **480** and **560** requests per second (RPS) on three distinct REST endpoints each. Note that the paper reports load levels as the total transactions per second across all three endpoints, i.e., three times the per-endpoint RPS used in the folder names:

| **Load Level (RPS per endpoint)** | **Total load (T/s, paper notation)** | **Description** |
| --- | --- | --- |
| 0 | 0 | System idle, no external load applied. Serves as the baseline for energy and performance measurements. Results in CPU utilization of approximately 0%. |
| 230 | 690 | Moderate load: all three REST endpoints are stressed with 230 RPS. Results in CPU utilization of about 28%. |
| 350 | 1050 | High load: all three REST endpoints are stressed with 350 RPS. Results in CPU utilization of roughly 49%. |
| 480 | 1440 | Very high load: all three REST endpoints are stressed with 480 RPS. Results in CPU utilization of around 75%. |
| 560 | 1680 | Maximum load: all three REST endpoints are stressed with 560 RPS. Results in CPU utilization of about 91%. |

The `RS2/` and `RS3/` load-distribution experiments only use the 350 RPS per endpoint (1050 T/s) load level, distributed across the two containers as described above.

## Python Scripts for Generating Figures and Tables

### Step 1: Extract the raw measurement archives (required)

The raw data is version-controlled as one `.zip` archive per load level and environment (VM, Container, RS2, RS3), so a fresh clone contains only the archives — the run directories the analysis scripts traverse do not exist yet. **The archives must be extracted in place before any analysis script is run**, each next to its archive and named after it (e.g., `EXPERIMENT_RESULTS/Container/350.zip` → `EXPERIMENT_RESULTS/Container/350/`). Without this step every script terminates reporting that it found no data.

```bash
# from the top-level folder of the repository
cd EXPERIMENT_RESULTS
find . -name "*.zip" \
     -not -name "joularjx-result*" \
     -not -name "vm_scaphandre_6s_measurement_intervals.zip" \
     -execdir unzip -n -q {} \;
cd ..
```

```powershell
# Windows PowerShell equivalent, from the top-level folder of the repository
Get-ChildItem EXPERIMENT_RESULTS -Recurse -Filter *.zip |
  Where-Object { $_.Name -notlike 'joularjx-result*' -and
                 $_.Name -ne 'vm_scaphandre_6s_measurement_intervals.zip' } |
  ForEach-Object { Expand-Archive -Path $_.FullName -DestinationPath $_.DirectoryName -Force }
```

Two kinds of archive are deliberately skipped:

- The nested `joularjx-result_*.zip` archives inside the run directories. [`shared.py`](./EXPERIMENT_RESULTS/shared.py) extracts those on demand while parsing, so they can stay packed.
- [`VM/vm_scaphandre_6s_measurement_intervals.zip`](./EXPERIMENT_RESULTS/VM/vm_scaphandre_6s_measurement_intervals.zip). Unlike every other archive, it does not wrap its contents in a run directory — it holds the timestamped scenario folders directly. Extracting it into `VM/` would make `build_run_dirs()` read those timestamps (`20260719_…`) as load levels and silently mix these supplementary runs into the regular VM results. Extract it to a scratch directory outside `EXPERIMENT_RESULTS/` if you want to inspect it.

The extracted directories are listed in [`.gitignore`](./.gitignore) and therefore stay untracked; the archives remain the single source of truth.

### Step 2: Install the analysis dependencies

```bash
pip install -r EXPERIMENT_RESULTS/requirements.txt
```

### Step 3: Run the scripts

This repository contains several Python scripts for processing, analyzing, and visualizing the experimental results. These scripts are located in the [`EXPERIMENT_RESULTS/`](./EXPERIMENT_RESULTS/) folder. Most of the scripts are designed to be executed from the top-level folder of the repository (e.g., `python ./EXPERIMENT_RESULTS/createCpuUtilizationTableForAllLoadLevelsAndScenarios.py`). The only exceptions are the `create_power_consumption_barchart.py` and `visualizePowerCapAsBoxplot.py` scripts, which need to be executed from within the [`EXPERIMENT_RESULTS/`](./EXPERIMENT_RESULTS/) folder.

All scripts locate their **input** data relative to their own file location, so it does not matter where they are started from. Their **output**, however, is written relative to the current working directory: most scripts write `./<name>.pdf` and therefore drop their figures into the folder you started them from, while the two exceptions above write `../<name>.pdf` — running those from the repository root would place the PDFs *outside* the repository, which is why they have to be started from inside `EXPERIMENT_RESULTS/`. Tables and statistics are printed to standard output rather than written to files.

| Script Name | Description |
| --- | --- |
| [`create_power_consumption_barchart.py`](./EXPERIMENT_RESULTS/create_power_consumption_barchart.py) | Processes measurement data and generates bar charts of power consumption for different loads and scenarios. |
| [`visualizeIdlePowerConsumptionAsBoxPlot.py`](./EXPERIMENT_RESULTS/visualizeIdlePowerConsumptionAsBoxPlot.py) | Visualizes idle power consumption as boxplots to compare baseline measurements. |
| [`visualizeLoadLevelContainerPowerConsumptionAsBoxplots.py`](./EXPERIMENT_RESULTS/visualizeLoadLevelContainerPowerConsumptionAsBoxplots.py) | Creates boxplots of container-level power consumption across different load levels. |
| [`visualizeLoadLevelProcessPowerConsumptionAsBoxplots.py`](./EXPERIMENT_RESULTS/visualizeLoadLevelProcessPowerConsumptionAsBoxplots.py) | Creates boxplots of process-level power consumption across different load levels. |
| [`fig_rs2_rs3.py`](./EXPERIMENT_RESULTS/fig_rs2_rs3.py) | Creates process-level power consumption boxplots for the RS2 and RS3 experiments (impact of load distribution on accuracy in multi-container scenarios). |
| [`visualizeLoadLevelSystemPowerConsumptionAsBoxplots.py`](./EXPERIMENT_RESULTS/visualizeLoadLevelSystemPowerConsumptionAsBoxplots.py) | Creates boxplots of system-level power consumption across different load levels. |
| [`visualizeLoadLevelTransactionPowerConsumptionAsBoxplots.py`](./EXPERIMENT_RESULTS/visualizeLoadLevelTransactionPowerConsumptionAsBoxplots.py) | Visualizes transaction-level power consumption as boxplots for each load scenario. |
| [`visualizePowerCapAsBoxplot.py`](./EXPERIMENT_RESULTS/visualizePowerCapAsBoxplot.py) | Visualizes power cap measurements as boxplots. |
| [`createCpuUtilizationTableForAllLoadLevelsAndScenarios.py`](./EXPERIMENT_RESULTS/createCpuUtilizationTableForAllLoadLevelsAndScenarios.py) | Generates tables summarizing CPU utilization for all load levels and scenarios. |
| [`createResponseTimeTableForAllLoadLevelsAndScenarios.py`](./EXPERIMENT_RESULTS/createResponseTimeTableForAllLoadLevelsAndScenarios.py) | Generates tables summarizing the client-observed JMeter response times per HTTP method for all load levels and scenarios (response-time overhead evaluation). |
| [`count_jmeter_failures_by_load_and_tool.py`](./EXPERIMENT_RESULTS/count_jmeter_failures_by_load_and_tool.py) | Iterates all load levels and tool scenarios and reports failed JMeter requests. Only result files that actually contain failures are printed, so an environment listed with no lines beneath it had no failed requests. |
| [`recalculate_smartwatts_results_by_load_and_run.py`](./EXPERIMENT_RESULTS/recalculate_smartwatts_results_by_load_and_run.py) | Recomputes missing or incomplete SmartWatts results from the downloaded PowerAPI sensor reports, in parallel across scenarios. **Extra prerequisites** — see the note below the table. |
| [`statistical_appendix.py`](./EXPERIMENT_RESULTS/statistical_appendix.py) | Generates the consolidated statistical appendix for cross-table claims (e.g., tool accuracy vs. external-meter ground truth, Container vs. VM comparisons) that back the paper's headline results. |
| [`characterize_workload_resource_profile.py`](./EXPERIMENT_RESULTS/characterize_workload_resource_profile.py) | Quantifies the test application's per-request resource-use profile (CPU time, memory allocation, disk/network I/O) per HTTP method, using OTJAE's per-transaction resource-demand instrumentation. |
| [`shared.py`](./EXPERIMENT_RESULTS/shared.py) | Shared helper library used by all analysis scripts: directory traversal for the run/scenario structure described above, measurement file parsing, steady-state trimming, per-repetition aggregation, and the statistical helpers (Cohen's d, exact Wilcoxon signed-rank and Mann-Whitney U tests). Not executed directly. |

Two scripts accept command line options; all others take no arguments:

- `visualizeLoadLevelContainerPowerConsumptionAsBoxplots.py --environment Container VM` restricts the run to the named environments (all discovered environments by default).
- `recalculate_smartwatts_results_by_load_and_run.py` offers `--dry-run`, `--force-remove`, `--stop-on-error`, `--environment`, `--max-workers`, `--engine`, `--wsl-distro`, and `--wsl-python`. Run it with `--help` for the full descriptions.

> [!IMPORTANT]
> **Extra prerequisites for `recalculate_smartwatts_results_by_load_and_run.py`.** Unlike the other analysis scripts, this one does not just read the shipped data — it re-runs the SmartWatts formula over the raw PowerAPI sensor reports, which requires one of two external execution engines:
>
> - `--engine podman` (default) needs [Podman](https://podman.io/) installed; it runs the containerized `powerapi/smartwatts-formula` image.
> - `--engine wsl` needs a WSL2 distribution with `pip install smartwatts` performed inside it. This engine exists because SmartWatts' actor IPC uses ZeroMQ `ipc://` Unix domain sockets, which native Windows Python cannot use. Because that socket path is fixed rather than per-process, this engine always runs scenarios serially and ignores `--max-workers`.
>
> **You do not need either engine to reproduce the paper's results**: the SmartWatts outputs are already contained in the shipped `spring_docker_powerapi*` run archives, and every other script reads them directly. This script is only needed if you re-run the PowerAPI experiments yourself, or want to verify the offline attribution step.

### Mapping to the Figures and Tables of the Paper

The scripts write environment-suffixed file names (e.g. `..._Container_all_loads.pdf`), whereas some figures were included in the manuscript under a shortened name. The following table maps each figure and table of the paper to the script that produces it:

| **Paper artifact** | **Produced by** | **Output** |
| --- | --- | --- |
| Fig. "Idle power consumption" | [`visualizeIdlePowerConsumptionAsBoxPlot.py`](./EXPERIMENT_RESULTS/visualizeIdlePowerConsumptionAsBoxPlot.py) | `idle_power_consumption_boxplot_<env>.pdf` |
| Fig. "System power depending on utilization" | [`visualizeLoadLevelSystemPowerConsumptionAsBoxplots.py`](./EXPERIMENT_RESULTS/visualizeLoadLevelSystemPowerConsumptionAsBoxplots.py) | `power_consumption_boxplots_<env>_all_loads.pdf` |
| Fig. "Delta power depending on load level" | [`visualizeLoadLevelSystemPowerConsumptionAsBoxplots.py`](./EXPERIMENT_RESULTS/visualizeLoadLevelSystemPowerConsumptionAsBoxplots.py) | `delta_power_vs_loadlevel_<env>.pdf` |
| Figs. "Container-level power consumption by load" (Container and VM) | [`visualizeLoadLevelContainerPowerConsumptionAsBoxplots.py`](./EXPERIMENT_RESULTS/visualizeLoadLevelContainerPowerConsumptionAsBoxplots.py) | `container_power_consumption_boxplots_<env>_all_loads.pdf` |
| Figs. "Process-level power consumption by load" (Container and VM) | [`visualizeLoadLevelProcessPowerConsumptionAsBoxplots.py`](./EXPERIMENT_RESULTS/visualizeLoadLevelProcessPowerConsumptionAsBoxplots.py) | `process_power_consumption_boxplots_<env>_all_loads.pdf` |
| Figs. "Transaction-level power consumption by load" (Container and VM) | [`visualizeLoadLevelTransactionPowerConsumptionAsBoxplots.py`](./EXPERIMENT_RESULTS/visualizeLoadLevelTransactionPowerConsumptionAsBoxplots.py) | `transaction_power_consumption_boxplots_<env>_all_loads.pdf` |
| Figs. "Process-level power consumption with distributed load" (RS2 and RS3) | [`fig_rs2_rs3.py`](./EXPERIMENT_RESULTS/fig_rs2_rs3.py) | `process_power_consumption_boxplots_RS2_all_loads.pdf`, `..._RS3_all_loads.pdf` |
| Fig. "Power distribution of experiment runs" | [`visualizePowerCapAsBoxplot.py`](./EXPERIMENT_RESULTS/visualizePowerCapAsBoxplot.py) | `boxplot_total_power_by_load_and_run_<env>.pdf` |
| Fig. "Percentage of overall power draw" (Discussion) | [`create_power_consumption_barchart.py`](./EXPERIMENT_RESULTS/create_power_consumption_barchart.py) | `power_consumption_combined_barchart.pdf` |
| Tables "Container-level power depending on throughput" (Container and VM) | [`visualizeLoadLevelContainerPowerConsumptionAsBoxplots.py`](./EXPERIMENT_RESULTS/visualizeLoadLevelContainerPowerConsumptionAsBoxplots.py) | printed to stdout |
| Tables "Process-level power depending on throughput" (Container and VM) | [`visualizeLoadLevelProcessPowerConsumptionAsBoxplots.py`](./EXPERIMENT_RESULTS/visualizeLoadLevelProcessPowerConsumptionAsBoxplots.py) | printed to stdout |
| Tables "Power consumption per transaction" (Container and VM) | [`visualizeLoadLevelTransactionPowerConsumptionAsBoxplots.py`](./EXPERIMENT_RESULTS/visualizeLoadLevelTransactionPowerConsumptionAsBoxplots.py) | printed to stdout |
| Table "RS2/RS3 load distribution" | [`fig_rs2_rs3.py`](./EXPERIMENT_RESULTS/fig_rs2_rs3.py) | printed to stdout |
| Table "Mean system power consumption by load level" | [`visualizeLoadLevelSystemPowerConsumptionAsBoxplots.py`](./EXPERIMENT_RESULTS/visualizeLoadLevelSystemPowerConsumptionAsBoxplots.py) | printed to stdout |
| Table "Mean CPU utilization by load level and test setup" (overhead evaluation) | [`createCpuUtilizationTableForAllLoadLevelsAndScenarios.py`](./EXPERIMENT_RESULTS/createCpuUtilizationTableForAllLoadLevelsAndScenarios.py) | printed to stdout |
| Table "Response time overhead" | [`createResponseTimeTableForAllLoadLevelsAndScenarios.py`](./EXPERIMENT_RESULTS/createResponseTimeTableForAllLoadLevelsAndScenarios.py) | printed to stdout |
| Appendix table "Workload resource profile" | [`characterize_workload_resource_profile.py`](./EXPERIMENT_RESULTS/characterize_workload_resource_profile.py) | printed to stdout |
| Significance tests quoted throughout the results section | [`statistical_appendix.py`](./EXPERIMENT_RESULTS/statistical_appendix.py) | printed to stdout |
| Supporting check that no load level suffered failed requests | [`count_jmeter_failures_by_load_and_tool.py`](./EXPERIMENT_RESULTS/count_jmeter_failures_by_load_and_tool.py) | printed to stdout |

Note that [`create_power_consumption_barchart.py`](./EXPERIMENT_RESULTS/create_power_consumption_barchart.py) does not recompute all of its inputs. Its `SCENARIO_CONSTANTS` dictionary holds point estimates that were transcribed by hand from the output of the container-, process-, and transaction-level scripts listed above; if you regenerate those results, the constants have to be updated alongside them.

The remaining figures of the paper (the attribution-model illustrations, the experiment setup, and the runtime setups) are not generated from measurement data. Their sources are kept with the manuscript rather than in this package.

## Notes

- The configurations and scripts are designed to be flexible for different measurement and analysis scenarios.
- Administrator rights may be required for using Docker and running experiments.
- Detailed descriptions of the experiments, measurements, and analyses can be found in the paper.

## Contact

For questions regarding replication or use of the data, please contact the authors of the paper.