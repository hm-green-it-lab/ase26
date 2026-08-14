# Experiment automation with "reader-flow"

**`reader-flow`** is a Python-based orchestration tool for coordinating energy and performance measurement experiments across distributed systems.  
It integrates power and performance readers (e.g., RAPL, ProcFS, SNMP/Rittal), load generators (JMeter), and service lifecycle hooks to enable reproducible, parameterized, and synchronized measurement scenarios.

## 🎯 Goals

- Modular orchestration of **energy and performance measurements**
- **YAML-based configuration** for reproducibility and flexibility
- Remote `.jar` execution via **SSH**
- Local and remote power logging with synchronized start/stop
- Parallel execution of multiple measurement tools
- Integrated **JMeter load testing** with automatic artifact collection

## 🧰 Requirements

- Python **3.11+** recommended (matching the top-level README)
- Java (for `.jar` execution)

### 🔧 Install Python dependencies

```bash
pip install -r requirements.txt
```

> [!NOTE]
> This installs only what the automation itself needs (`python-dotenv`, `pyyaml`, `paramiko`). The analysis and visualization scripts have their own dependency list in [`../EXPERIMENT_RESULTS/requirements.txt`](../EXPERIMENT_RESULTS/requirements.txt).

### 🌐 Set environment variables

> [!INFO]
> Adjust both `.env` and `paths.env` before running experiments.
>
> 1. Create `.env` from `.env-template` and set SSH credentials:
>    - `SUT_SSH_USER`, `SUT_SSH_PASSWORD`
>    - `JMETER_SSH_USER`, `JMETER_SSH_PASSWORD`
>    - `VM_SSH_USER`, `VM_SSH_PASSWORD` (for `spring_vm_*` experiments)
> 2. Update `paths.env` with host/path placeholders used by YAML files:
>    - `SUT_HOST`, `SUT_BASE_DIR`
>    - `JMETER_HOST`, `JMETER_BASE_DIR`
>    - `LOCAL_BASE_DIR`
>    - `VM_HOST`, `VM_PORT`, `VM_BASE_DIR` (for `spring_vm_*` experiments; `VM_HOST`/`VM_PORT` address the guest through the QEMU SSH port forward set up in [`vms/`](./vms/), by default `127.0.0.1:2222`)
> 3. In `paths.env`, set jar download URLs (`RITTAL_JAR_URL`, `HTTP_LOGGER_JAR_URL`, `PROCFS_JAR_URL`, `POWERCAP_JAR_URL`) if you want automatic jar download when files are missing.
> 4. In `paths.env`, set the Rittal PDU SNMP connection (`RITTAL_SNMP_ADDRESS`, `RITTAL_SNMP_COMMUNITY`, `RITTAL_SNMP_OIDS`) used by the local rittal-reader JAR.

## 🔁 What happens automatically on each run

Before dispatching an experiment, the automation:

- ensures required local JARs exist (downloads them if URLs are configured),
- ensures remote-reader JARs exist locally in `./tools`,
- syncs `./docker` to the SUT under `${SUT_BASE_DIR}/spring-rest-service`,
- syncs `./vms` to `${SUT_BASE_DIR}/vm`,
- uploads remote reader JARs (e.g. ProcFS/Powercap) to `experiment.remote_dir` on the SUT.

So for normal runs you do not need to manually copy docker files or remote reader jars to the SUT before each execution.

## 🔬 Supported Experiment Types

### Baseline Idle Measurement - No Tools

 - Idle system measurements (no workload)
 - Runs only local Rittal SNMP reader

### Baseline Idle Measurement

 - Idle system measurements (no workload)
 - Runs remote Powercap reader (RAPL), ProcFS reader, and local Rittal SNMP reader

### Spring REST Application Idle Measurement - Docker

- Idle Spring REST application with docker deployment measurements (no workload)
- Runs remote Powercap reader (RAPL), ProcFS reader, and local Rittal SNMP reader

### Spring REST Application Load Measurement - Docker

- JMeter (optional) load Spring REST application with docker deployment measurements
- Runs remote Powercap reader (RAPL), ProcFS reader, local Rittal SNMP reader, and respective measurement tools

### Spring REST Application Load Measurement - VM (`spring_vm_*` configs)

- Same as the Docker load measurement, but the application container runs inside a QEMU/KVM guest VM on the SUT
- Requires a prepared VM disk image (see [`VM_setup.md`](./VM_setup.md)) and the scripts in [`vms/`](./vms/) to start/control the VM
- For Scaphandre and JoularJX/PowerJoular, an additional host-side instance attributes power to the VM process and shares it with the guest

### Multi-Container Load Distribution (`*_rs2.yml` / `*_rs3.yml` configs)

- Two co-located instances of the application run in separate containers, receiving a fixed total load distributed in 50/50, 67/33, or 80/20 splits
- `*_rs2.yml`: each container is pinned to one CPU socket/NUMA region via `docker update --cpuset-cpus`/`--cpuset-mems` (dedicated resources; adjust the hardcoded bindings to your CPU topology)
- `*_rs3.yml`: both containers share the full hardware without pinning (shared resources)

> [!TIP]
> See the [`configuration/`](./configuration/) folder for example configuration files.

### Configuration file inheritance

Configuration files are composed rather than duplicated. Each tool-specific YAML file starts with an `extends:` key naming a base configuration, and its own keys are then deep-merged on top of that base (see `load_config` and `merge_configs` in [`main.py`](./main.py)). `extends` is resolved recursively and rejects circular inheritance. Placeholders of the form `${VAR}` are substituted from `paths.env` as each file is read, before the merge; SSH credentials from `.env` are read separately at runtime and are not available as YAML placeholders.

The three base configurations are:

| Base configuration | Extended by | Contains |
| --- | --- | --- |
| [`spring_docker_jmeter.yml`](./configuration/spring_docker_jmeter.yml) | all `spring_docker_*` configs | Common reader/JMeter/output settings for the Container environment. |
| [`spring_vm_jmeter.yml`](./configuration/spring_vm_jmeter.yml) | all `spring_vm_*` configs | The same, for the VM environment, plus the guest VM lifecycle settings. |
| [`baseline_idle.yml`](./configuration/baseline_idle.yml) | [`baseline_idle_no_tools.yml`](./configuration/baseline_idle_no_tools.yml) | Idle measurement with the ProcFS/Powercap/Rittal readers. |

So to change something for every Container experiment at once — the measurement duration, for instance — edit `spring_docker_jmeter.yml` rather than each tool configuration.

## 🏃 Running Experiments

### Baseline measurements

```bash
python main.py --config "[PATH]/configuration/[configuration].yml"
```

You can also use a wrapper script to run multiple configurations back-to-back:

> [!TIP]
> See [`run.ps1`](./run.ps1) (Windows PowerShell) or [`run.sh`](./run.sh) (Linux/bash) for examples of sweeping several experiment configurations and load levels in sequence. Note that the two scripts are examples with different hardcoded load levels/repetitions/configs tailored to specific experiment runs — adjust the variables at the top of the script to match the configurations you want to run.

## 📂 Output Files

Each experiment run creates output files and folders in the following format:

- `{YYYYMMDD}_{HHMMSS}_{configuration}/` – All measurement and result files for a single experiment run are grouped in a timestamped folder named by date, time, and configuration.

Additionally, each folder includes a `logs/experiment_log.jsonl` file containing metadata (PIDs, temperatures, durations, file sizes) for the experiment.

## 🧵 Parallel Measurements

- Powercap (RAPL)
- ProcFS reader (CPU, I/O, network, memory)
- Rittal SNMP reader
- JMeter (optional)

All tools run in parallel threads, ensuring synchronized measurement windows. Console logs indicate start/stop events immediately, but measurement readers continue for the configured duration.

## 🧪 Debugging Tips

If a script hangs or becomes unresponsive:

- Press `Ctrl + C` to cancel the execution manually.
- On Windows with PowerShell:
  ```powershell
  Get-Process python | Stop-Process -Force
  ```
- On remote host:
  ```bash
  ps aux | grep java
  sudo kill <PID>
  ```

To check if the Spring REST application is actually running:

```
curl -i http://localhost:8081/test-rest-endpoint/getData
curl -i -X POST http://localhost:8081/test-rest-endpoint/postData
curl -i -X DELETE http://localhost:8081/test-rest-endpoint/deleteData
```

## ⚠️ Known Issues & Tips

> [!CAUTION]
> Always check VPN connection first 😉!

- On Windows, Java processes launched for Rittal SNMP reading may not respond to normal termination signals.
  - **reader-flow** uses `taskkill /F /T` to ensure they're properly killed.
- Ensure `lm-sensors` is installed on the remote machine if temperature logging is enabled.
- `check_remote_clock_drift` validates the remote time against the local host, and aborts the experiment if drift exceeds **2 seconds** (default threshold).