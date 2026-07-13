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

- Python **3.10+** recommended
- Java (for `.jar` execution)

### 🔧 Install Python dependencies

```bash
pip install -r requirements.txt
```

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
> 3. In `paths.env`, set jar download URLs (`RITTAL_JAR_URL`, `HTTP_LOGGER_JAR_URL`, `PROCFS_JAR_URL`, `POWERCAP_JAR_URL`) if you want automatic jar download when files are missing.

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

> [!TIP]
> See the [`configuration/`](./configuration/) folder for example configuration files.

## 🏃 Running Experiments

### Baseline measurements

```bash
python main.py --config "[PATH]/configuration/[configuration].yml"
```

You can also use a PowerShell script on Windows to run multiple configurations:

> [!TIP]
> See the [`run.ps1`](./run.ps1) script for an example of running multiple experiment configurations on Windows.

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