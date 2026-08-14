# Run several experiment configurations back-to-back (Windows PowerShell
# counterpart of run.sh).
#
# This is an EXAMPLE sweep, not a fixed protocol: the load levels, repetition
# count, and configuration list below are the ones used for one particular
# measurement campaign. Adjust the variables in the block underneath to match
# the configurations you want to run.
#
# Each iteration invokes `main.py --config <configuration>` and then waits
# $waitBetweenRunsSeconds before the next one, so the machine returns to a
# comparable thermal state between measurements.
#
# Prerequisites: `.env` and `paths.env` configured, and the SUT and JMeter load
# driver set up (see setup/01_README.md).
$scriptPath = Join-Path $PSScriptRoot "main.py"
$configDir = Join-Path $PSScriptRoot "configuration"
$pythonCmd = Get-Command python -ErrorAction SilentlyContinue
if (-not $pythonCmd) {
    throw "Python executable not found in PATH. Activate your venv or install Python."
}
$pythonExe = $pythonCmd.Source

$waitBetweenRunsSeconds = 300
$repetitionsPerLoad = 1
$loadLevels = @(350)


# Configs that use JMeter and should run for every load level.
$loadSweepConfigs = @(
    "spring_docker_powerapi.yml"
)

foreach ($configFile in $loadSweepConfigs) {
    $configPath = Join-Path $configDir $configFile

    foreach ($loadLevel in $loadLevels) {
        foreach ($rep in 1..$repetitionsPerLoad) {
            Write-Host "`nRunning $configFile | load=$loadLevel | repetition=$rep/$repetitionsPerLoad`n" -ForegroundColor Green

            & $pythonExe $scriptPath --config $configPath --total-rate $loadLevel
            if ($LASTEXITCODE -ne 0) {
                throw "Experiment failed for config '$configFile' (load=$loadLevel rep=$rep) with exit code $LASTEXITCODE"
            }

            Write-Host "`nWaiting 5 minutes before next run...`n" -ForegroundColor Yellow
            Start-Sleep -Seconds $waitBetweenRunsSeconds
        }
    }
}