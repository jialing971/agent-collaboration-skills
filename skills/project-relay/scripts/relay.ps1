param(
    [Parameter(Mandatory = $true)][string]$Project,
    [Parameter(ValueFromRemainingArguments = $true)][string[]]$RelayArguments
)
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
$taskPython = $env:PROJECT_RELAY_PYTHON
if ([string]::IsNullOrEmpty($taskPython)) {
    $taskPython = Join-Path $env:USERPROFILE '.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'
    if (-not (Test-Path -LiteralPath $taskPython -PathType Leaf)) {
        throw "Codex 自带 Python 不存在（$taskPython）；请设置 PROJECT_RELAY_PYTHON 指向 Python 3.10+，不回退到其他运行时。"
    }
} elseif (-not (Test-Path -LiteralPath $taskPython -PathType Leaf)) {
    throw "PROJECT_RELAY_PYTHON 指定的 Python 不存在：$taskPython；请修正该环境变量，不回退到其他运行时。"
}
$taskScript = Join-Path $PSScriptRoot 'relay.py'
& $taskPython -X utf8 $taskScript --project $Project @RelayArguments
exit $LASTEXITCODE
