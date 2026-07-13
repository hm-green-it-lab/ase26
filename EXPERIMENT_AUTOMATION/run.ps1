# Run all experiment configs from this repository checkout.
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