# scripts/run_fast_tests.ps1
# Fast pre-commit test runner for rapid vibe coding loop (< 15s)
[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"

Write-Host "=== Running Fast Regression Suite (Unit & Safety Gates) ===" -ForegroundColor Cyan

$testFiles = @(
    "tests/test_core_safety.py",
    "tests/test_status_probe.py",
    "tests/test_execution_security.py",
    "tests/test_fsm.py",
    "tests/test_budget_guard.py",
    "tests/test_engine_selector.py"
)

python -m pytest $testFiles -q -p no:cacheprovider
if ($LASTEXITCODE -eq 0) {
    Write-Host "`n[PASS] Fast regression suite completed successfully in < 15s." -ForegroundColor Green
} else {
    Write-Host "`n[FAIL] Fast regression suite failed with exit code $LASTEXITCODE." -ForegroundColor Red
    exit $LASTEXITCODE
}
