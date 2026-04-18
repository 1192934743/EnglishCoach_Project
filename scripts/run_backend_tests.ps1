# Run from repo root: .\scripts\run_backend_tests.ps1
# Optional: -Integration to include WebSocket tests
param([switch]$Integration)
$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot ".." "python_backend")
python -m pip install -q -r requirements.txt -r requirements-dev.txt 2>$null
if ($Integration) {
  python -m pytest tests -q --tb=short
} else {
  python -m pytest tests -m "not integration" -q --tb=short
}
