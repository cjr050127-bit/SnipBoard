$env:PYTHONPATH = Join-Path $PSScriptRoot 'src'
$python = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python)) { $python = 'python' }
& $python -m unittest discover -s (Join-Path $PSScriptRoot 'tests') -v
exit $LASTEXITCODE
