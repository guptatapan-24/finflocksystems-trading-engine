# Runs the whole Flink state demo: baseline (pre-fix) and fixed, each on a fresh
# stack, then prints the comparison. Run from the backend folder:
#   powershell -ExecutionPolicy Bypass -File demo\flink_state\run-demo.ps1
$ErrorActionPreference = "Stop"
$compose = @("compose", "-f", "docker-compose.flink-demo.yml")

function Invoke-Compose {
    & docker @compose @args
    if ($LASTEXITCODE -ne 0) { throw "docker $($compose -join ' ') $($args -join ' ') failed ($LASTEXITCODE)" }
}

Invoke-Compose build demo
foreach ($variant in @("baseline", "fixed")) {
    Write-Host "`n=================== $variant ===================`n"
    Invoke-Compose down --remove-orphans
    try {
        Invoke-Compose run --rm demo $variant
    } finally {
        & docker @compose down --remove-orphans
    }
}
Invoke-Compose run --rm --no-deps demo compare
