param(
    [ValidateSet('serve', 'query', 'test', 'eval', 'export')]
    [string]$Mode = 'serve',
    [string]$Query = 'C001 enterprise profile',
    [int]$Port = 8018,
    [string]$Entity = '',
    [ValidateSet('sft', 'rl', 'raw')]
    [string]$Format = 'sft'
)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $projectRoot
$env:PYTHONIOENCODING = 'utf-8'
$runtimePath = Join-Path $env:USERPROFILE '.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'
$pythonCommand = Get-Command python -ErrorAction SilentlyContinue
if ($pythonCommand) {
    $runtimePath = $pythonCommand.Source
} elseif (-not (Test-Path -LiteralPath $runtimePath)) {
    throw 'Python 3.10+ is required. Install Python and run: python -m audit_assistant --serve --port 8018'
}
switch ($Mode) {
    'serve' { & $runtimePath -m audit_assistant --serve --port $Port }
    'query' {
        if ($Entity) { & $runtimePath -m audit_assistant --query $Query --entity $Entity }
        else { & $runtimePath -m audit_assistant --query $Query }
    }
    'test' { & $runtimePath -m unittest discover -s tests -v }
    'eval' { & $runtimePath -m audit_assistant.evaluate --output artifacts/evaluation.json }
    'export' { & $runtimePath -m audit_assistant --export $Format --output "artifacts/$Format.jsonl" }
}
exit $LASTEXITCODE
