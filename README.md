# Evaluating attribution models and tools for software energy consumption at virtual machine, container, process, and transaction levels – Replication Package

This repository contains the data, scripts, and configuration files required to replicate the experiments from the paper. The structure and contents are aligned with the experiments and analyses described in the paper.

## Folder Structure

Key directories and their contents:

- [`EXPERIMENT_AUTOMATION/`](./EXPERIMENT_AUTOMATION/) – Automation scripts and configurations for running the experiments.
  - [`configuration/`](./EXPERIMENT_AUTOMATION/configuration/) – YAML configuration files for different experiment setups.
  - [`docker/`](./EXPERIMENT_AUTOMATION/docker/) – Docker Compose files and tool-specific configurations (server-side).
  - [`helper/`](./EXPERIMENT_AUTOMATION/helper/) – Python based helper scripts for measurements.
  - [`orchestrator/`](./EXPERIMENT_AUTOMATION/orchestrator/) – Python modules for controlling and evaluating measurements.
  - [`output/`](./EXPERIMENT_AUTOMATION/output/) – output files for the experiment runs.
  - [`.env-template`](./EXPERIMENT_AUTOMATION/.env-template) – Python .env template.
  - [`paths.env`](./EXPERIMENT_AUTOMATION/paths.env) – Hostnames, directories, tool download URLs, and path placeholders used by YAML configs.
  - [`run.ps1`](./EXPERIMENT_AUTOMATION/run.ps1) – Dedicated Windows PowerShell helper to execute multiple experiment configurations.
  - [`README.md`](./EXPERIMENT_AUTOMATION/README.md) – Dedicated README for experiment automation with Python.

- [`EXPERIMENT_RESULTS/`](./EXPERIMENT_RESULTS/) – Experiment raw results including Python scripts for analysis and visualization of measurement results, e.g., [`visualizeLoadLevelContainerPowerConsumptionAsBoxplots.py`](./EXPERIMENT_RESULTS/visualizeLoadLevelContainerPowerConsumptionAsBoxplots.py), [`visualizeIdlePowerConsumptionAsBoxPlot.py`](./EXPERIMENT_RESULTS/visualizeIdlePowerConsumptionAsBoxPlot.py), etc.

- [`requirements.txt`](./requirements) – List of dependencies for Python scripts.

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
| [JoularJX](https://github.com/joular/joularjx)                           | Java agent for measuring energy consumption of JVM-based applications at the process, thread, and method level.                                                                                                   |
| [OTJAE](https://github.com/RETIT/opentelemetry-javaagent-extension)      | OpenTelemetry Java-Agent Extension for attributing energy consumption to Java processes and transactions.                                                                                                         |
| [lm-sensors](https://github.com/lm-sensors/lm-sensors) | The lm-sensors package is used to measure the temperature of the CPU sockets before and after each test.                                                                                                          |

These tools were orchestrated and synchronized using the automation scripts described below to ensure reproducible and accurate measurements across all experiment runs. Before presenting the automation in detail, the next section explains the experiment initialization and setup required to prepare the servers and measurement environment.

## Environment Setup

To run the experiments using the experiment automations scripts (see[`EXPERIMENT_AUTOMATION/README.md`](./EXPERIMENT_AUTOMATION/README.md)), you need to prepare the system under test (SUT), the JMeter load driver, and your local environment. The following sections describe the required setup for each of these components.

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
  - Set tool URLs used for automatic download when jars are missing:
    - `RITTAL_JAR_URL`, `HTTP_LOGGER_JAR_URL`
    - `PROCFS_JAR_URL`, `POWERCAP_JAR_URL`
  - Set Rittal SNMP connection values:
    - `RITTAL_SNMP_ADDRESS`, `RITTAL_SNMP_COMMUNITY`, `RITTAL_SNMP_OIDS`

### System Under Test (SUT) Setup

On the SUT, install Docker and ensure the configured base directories are writable by the SSH user.

You do **not** need to manually copy files from [`./EXPERIMENT_AUTOMATION/docker/`](./EXPERIMENT_AUTOMATION/docker/) to the SUT before each run. During each experiment run, the automation:

- cleans `${SUT_BASE_DIR}` on the SUT,
- uploads `EXPERIMENT_AUTOMATION/docker/` to `${SUT_BASE_DIR}/spring-rest-service`,
- uploads `EXPERIMENT_AUTOMATION/vms/` to `${SUT_BASE_DIR}/vm`.

The docker command paths in the YAML files (for `remote_docker_start`, `remote_docker_stop`, and `remote_docker_logs`) are already aligned with this layout. The following is an example of the resulting folder structure on the SUT:

```
/home/user/spring-rest-service
├── docker-compose.override.joularjx.yaml
├── docker-compose.override.kepler.yaml
├── docker-compose.override.otel.yaml
├── docker-compose.override.scaphandre.yaml
├── docker-compose.yaml
├── joularjx
    ├── config.properties
    ├── joularjx-3.0.1.jar
    ├── joularjx-result
    ├── results
    └── zip
```

- `docker-compose.yaml` is the base compose file describing the Spring REST application.
- The `docker-compose.override.*.yaml` files provide tool-specific overrides. Each override file enables and configures one measurement tooling stack (for example, Kepler, Scaphandre, JoularJX, or OTJAE).
- The `joularjx/` directory contains the JoularJX agent jar and its configuration/results directories.

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

The Jmeter load test script for the experiments can be downloaded here: https://github.com/RETIT/opentelemetry-javaagent-extension/blob/v0.0.18-alpha/examples/spring-rest-service/src/test/resources/jmeter_testplan.jmx and can be placed on the JMeter load driver in the same directory as the JMeter binary. It is important to ensure that the load test script location is correctly specified in the `test_plan` attribute of the `jmeter` configuration:

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

The experiment automation scripts are inteneded to the run on your local environment. For details on running experiments, see the [`EXPERIMENT_AUTOMATION/README.md`](./EXPERIMENT_AUTOMATION/README.md) which describes usage, configuration, and automation scripts in depth.

## Experiment Results

This [folder](./EXPERIMENT_RESULTS/) contains the raw measurement data and analysis scripts for the experiments presented in our paper.

The experiments were conducted using a Spring REST application deployed in Docker, with energy and performance measurements taken under varying load levels. For each experiment run, the system was subjected to one of the following load intensities: **0**, **230**, **350**, **480** and **560** requests per second (RPS) on three distinct REST-endpoints each. All measurement data and results are organized by timestamp and configuration.

The Python scripts in the [`EXPERIMENT_RESULTS/`](./EXPERIMENT_RESULTS/) folder process the raw data and generate the figures and tables used in the paper.

| **Load Level (RPS)** | **Description** |
| --- | --- |
| 0 | System idle, no external load applied. Serves as the baseline for energy and performance measurements. Results in CPU utilization of approximately 0%. |
| 230 | Moderate load: all three REST endpoints are stressed with 230 RPS. Results in CPU utilization of about 25%. |
| 350 | High load: all three REST endpoints are stressed with 350 RPS. Results in CPU utilization of roughly 50%. |
| 480 | Very high load: all three REST endpoints are stressed with 480 RPS. Results in CPU utilization of around 75%. |
| 560 | Maximum load: all three REST endpoints are stressed with 560 RPS. Results in CPU utilization close to 100%. |

For each load level, the experiment was repeated three times to ensure validity. The results for each run are stored in dedicated `.zip` files within [`EXPERIMENT_RESULTS/`](./EXPERIMENT_RESULTS/), organized by load level and timestamps as mentioned. Each `.zip` archive contains all measurement data and logs for a single experiment run.

## Python Scripts for Generating Figures and Tables

This repository contains several Python scripts for processing, analyzing, and visualizing the experimental results. These scripts are located in the [`EXPERIMENT_RESULTS/`](./EXPERIMENT_RESULTS/) folder. Most of the scripts are designed to be executed from the top-level folder of the repository (e.g., `python ./EXPERIMENT_RESULTS/createCpuUtilizationTableForAllLoadLevelsAndScenarios.py`). The only exceptions are the `create_power_consumption_barchart.py` and `visualizePowerCapAsBoxplot.py` scripts, which need to be executed from within the [`EXPERIMENT_RESULTS/`](./EXPERIMENT_RESULTS/) folder. 

| Script Name | Description |
| --- | --- |
| [`create_power_consumption_barchart.py`](./EXPERIMENT_RESULTS/create_power_consumption_barchart.py) | Processes measurement data and generates bar charts of power consumption for different loads and scenarios. |
| [`visualizeIdlePowerConsumptionAsBoxPlot.py`](./EXPERIMENT_RESULTS/visualizeIdlePowerConsumptionAsBoxPlot.py) | Visualizes idle power consumption as boxplots to compare baseline measurements. |
| [`visualizeLoadLevelContainerPowerConsumptionAsBoxplots.py`](./EXPERIMENT_RESULTS/visualizeLoadLevelContainerPowerConsumptionAsBoxplots.py) | Creates boxplots of container-level power consumption across different load levels. |
| [`visualizeLoadLevelProcessPowerConsumptionAsBoxplots.py`](./EXPERIMENT_RESULTS/visualizeLoadLevelProcessPowerConsumptionAsBoxplots.py) | Creates boxplots of process-level power consumption across different load levels. |
| [`visualizeLoadLevelSystemPowerConsumptionAsBoxplots.py`](./EXPERIMENT_RESULTS/visualizeLoadLevelSystemPowerConsumptionAsBoxplots.py) | Creates boxplots of system-level power consumption across different load levels. |
| [`visualizeLoadLevelTransactionPowerConsumptionAsBoxplots.py`](./EXPERIMENT_RESULTS/visualizeLoadLevelTransactionPowerConsumptionAsBoxplots.py) | Visualizes transaction-level power consumption as boxplots for each load scenario. |
| [`visualizePowerCapAsBoxplot.py`](./EXPERIMENT_RESULTS/visualizePowerCapAsBoxplot.py) | Visualizes power cap measurements as boxplots. |
| [`createCpuUtilizationTableForAllLoadLevelsAndScenarios.py`](./EXPERIMENT_RESULTS/createCpuUtilizationTableForAllLoadLevelsAndScenarios.py) | Generates tables summarizing CPU utilization for all load levels and scenarios. |

## Notes

- The configurations and scripts are designed to be flexible for different measurement and analysis scenarios.
- Administrator rights may be required for using Docker and running experiments.
- Detailed descriptions of the experiments, measurements, and analyses can be found in the paper.

## Contact

For questions regarding replication or use of the data, please contact the authors of the paper.