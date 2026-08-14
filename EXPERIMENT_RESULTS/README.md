# Experiment Results

This folder holds the raw measurement data for all experiment runs together with the Python scripts that turn it into the figures and tables of the paper.

The full documentation lives in the [top-level README](../README.md):

- [Directory structure: environments and runtime setups](../README.md#directory-structure-environments-and-runtime-setups) — what `Container/`, `VM/`, `RS2/`, and `RS3/` contain and how the run folders are named.
- [Test setups: scenario folder names](../README.md#test-setups-scenario-folder-names) — how the `{YYYYMMDD}_{HHMMSS}_{configuration}` folders map to the paper's test setups TS1–TS6.
- [Load levels](../README.md#load-levels) — how the per-endpoint RPS in the folder names relates to the total T/s reported in the paper.
- [Python scripts for generating figures and tables](../README.md#python-scripts-for-generating-figures-and-tables) — what each script does, how to run it, and which paper figure or table it produces.

## Quick start

The raw data is version-controlled as one `.zip` archive per load level, so a fresh clone has no run directories yet. Extract them first, then install the dependencies:

```bash
# from this folder
find . -name "*.zip" \
     -not -name "joularjx-result*" \
     -not -name "vm_scaphandre_6s_measurement_intervals.zip" \
     -execdir unzip -n -q {} \;

pip install -r requirements.txt
```

Then run the scripts from the top-level folder of the repository:

```bash
cd ..
python ./EXPERIMENT_RESULTS/visualizeLoadLevelProcessPowerConsumptionAsBoxplots.py
```

`create_power_consumption_barchart.py` and `visualizePowerCapAsBoxplot.py` are the two exceptions — they write their output to `../`, so they have to be started from inside this folder. See the [top-level README](../README.md#step-3-run-the-scripts) for the details and for why `vm_scaphandre_6s_measurement_intervals.zip` must not be extracted here.
