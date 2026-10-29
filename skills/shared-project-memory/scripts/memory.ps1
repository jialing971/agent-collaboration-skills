# 使用 UTF-8 BOM 兼容 Windows PowerShell 5.1。
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding $false
$memoryPython = $env:SHARED_PROJECT_MEMORY_PYTHON
if ([string]::IsNullOrEmpty($memoryPython)) {
    $memoryPython = Join-Path $env:USERPROFILE '.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'
    if (-not (Test-Path -LiteralPath $memoryPython -PathType Leaf)) {
        [Console]::Error.WriteLine('Codex 自带 Python 不存在；请设置 SHARED_PROJECT_MEMORY_PYTHON 指向已验证支持中文参数的 Python 3.10+，不回退到其他运行时。')
        exit 1
    }
} elseif (-not (Test-Path -LiteralPath $memoryPython -PathType Leaf)) {
    [Console]::Error.WriteLine("SHARED_PROJECT_MEMORY_PYTHON 指定的 Python 不存在：$memoryPython；请修正该环境变量，不回退到其他运行时。")
    exit 1
}
& $memoryPython -X utf8 (Join-Path $PSScriptRoot 'memory.py') @args
exit $LASTEXITCODE
