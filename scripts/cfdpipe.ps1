[CmdletBinding()]
param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]] $CfdpipeArguments
)

$ErrorActionPreference = "Stop"
$repositoryRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$pythonExecutable = Join-Path $repositoryRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $pythonExecutable -PathType Leaf)) {
    throw "Repository Python was not found at $pythonExecutable"
}

$sourceDirectory = Join-Path $repositoryRoot "src"
$previousPythonPath = [Environment]::GetEnvironmentVariable(
    "PYTHONPATH",
    "Process"
)
$exitCode = 1
try {
    if ([string]::IsNullOrWhiteSpace($previousPythonPath)) {
        $env:PYTHONPATH = $sourceDirectory
    }
    else {
        $env:PYTHONPATH = $sourceDirectory + [IO.Path]::PathSeparator + $previousPythonPath
    }
    & $pythonExecutable -m cfdpipe @CfdpipeArguments
    $exitCode = $LASTEXITCODE
}
finally {
    [Environment]::SetEnvironmentVariable(
        "PYTHONPATH",
        $previousPythonPath,
        "Process"
    )
}

exit $exitCode
