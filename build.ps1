param([switch]$InstallPyInstaller, [switch]$SkipTests)
$ErrorActionPreference = 'Stop'
$project = $PSScriptRoot
$python = Join-Path $project '.venv\Scripts\python.exe'
$originalPath = $env:PATH
if (-not (Test-Path -LiteralPath $python)) { throw '请先创建 .venv 并安装 requirements-lock.txt。' }
Push-Location $project
try {
    $env:PYTHONPATH = Join-Path $project 'src'
    if (-not $SkipTests) {
        & $python -m unittest discover -s tests -q
        if ($LASTEXITCODE -ne 0) { throw '测试失败，停止打包。' }
    }
    & $python -c 'import PyInstaller'
    if ($LASTEXITCODE -ne 0) {
        if (-not $InstallPyInstaller) { throw 'PyInstaller 未安装。可加 -InstallPyInstaller 安装。' }
        & $python -m pip install -r requirements-build.txt
        if ($LASTEXITCODE -ne 0) { throw 'PyInstaller 安装失败。' }
    }
    $env:PATH = @((Join-Path $env:SystemRoot 'System32'), $env:SystemRoot, (Split-Path $python)) -join ';'
    & $python -m PyInstaller --noconfirm --clean SnipBoard-Qt.spec
    if ($LASTEXITCODE -ne 0) { throw '打包失败。' }
    $exe = Join-Path $project 'dist\SnipBoard-0.6.3\SnipBoard.exe'
    $selfTestRoot = Join-Path $project ('build\qt-selftest-' + [guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory -Path $selfTestRoot | Out-Null
    try {
        $process = Start-Process -FilePath $exe -ArgumentList @('--self-test', '--library', $selfTestRoot) -PassThru -WindowStyle Hidden
        if (-not $process.WaitForExit(30000)) {
            Stop-Process -Id $process.Id -Force
            throw '打包自检超时，已停止本次自检进程。'
        }
        if ($process.ExitCode -ne 0) { throw "打包自检失败：$($process.ExitCode)" }
    }
    finally {
        $resolved = [IO.Path]::GetFullPath($selfTestRoot)
        $allowed = [IO.Path]::GetFullPath((Join-Path $project 'build')) + [IO.Path]::DirectorySeparatorChar
        if (-not $resolved.StartsWith($allowed, [StringComparison]::OrdinalIgnoreCase)) { throw '拒绝清理越界目录。' }
        if (Test-Path -LiteralPath $resolved) { Remove-Item -LiteralPath $resolved -Recurse -Force }
    }
    $hash = (Get-FileHash -LiteralPath $exe -Algorithm SHA256).Hash
    Set-Content -LiteralPath (Join-Path $project 'dist\SnipBoard-0.6.3\SHA256SUMS.txt') -Value "$hash *SnipBoard.exe" -Encoding ascii
    $releaseDirectory = Split-Path $exe
    Copy-Item -LiteralPath (Join-Path $project 'README.md') -Destination (Join-Path $releaseDirectory '使用说明.md')
    $releaseDocs = Join-Path $releaseDirectory 'docs'
    New-Item -ItemType Directory -Path $releaseDocs -Force | Out-Null
    foreach ($name in @('USER_GUIDE.md', 'PRIVACY.md', 'COMPATIBILITY_AND_LEGAL.md', 'DEPENDENCY_SOURCES.json', 'RELEASE_0_6_3.md', 'RELEASE_0_6_2.md', 'RELEASE_0_6_1.md', 'RELEASE_0_6_0.md', 'RELEASE_0_5_5.md')) {
        Copy-Item -LiteralPath (Join-Path $project ('docs/' + $name)) -Destination $releaseDocs
    }
    Copy-Item -LiteralPath (Join-Path $project 'docs/images') -Destination $releaseDocs -Recurse -Force
    Copy-Item -LiteralPath (Join-Path $project 'assets') -Destination $releaseDirectory -Recurse -Force
    & $python tools/release_manifest.py (Split-Path $exe)
    if ($LASTEXITCODE -ne 0) { throw '发布文件清单生成失败。' }
    Write-Output "BUILD_OK=$exe"
    Write-Output "SHA256=$hash"
}
finally { $env:PATH = $originalPath; Pop-Location }
