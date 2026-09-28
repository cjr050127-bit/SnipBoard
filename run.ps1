param([Parameter(ValueFromRemainingArguments = $true)][string[]]$Arguments)
$env:PYTHONPATH = Join-Path $PSScriptRoot 'src'
$python = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python)) { $python = 'python' }
& $python -m snipboard @Arguments
exit $LASTEXITCODE
