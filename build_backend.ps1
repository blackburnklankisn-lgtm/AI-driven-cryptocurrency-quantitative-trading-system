# ============================================================================
# AI Quant Trader - 后端构建脚本 (快速模式)
# ============================================================================
# 功能: 仅构建后端 Python 可执行程序
# 用法: powershell -ExecutionPolicy Bypass -File build_backend.ps1
# ============================================================================

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

Write-Host "╔════════════════════════════════════════════════════════════════════════════╗" -ForegroundColor Cyan
Write-Host "║              后端 Python 可执行程序构建 (PyInstaller)                       ║" -ForegroundColor Cyan
Write-Host "╚════════════════════════════════════════════════════════════════════════════╝" -ForegroundColor Cyan
Write-Host ""

# 检查 PyInstaller
try {
    $pyinstallerVersion = pyinstaller --version
    Write-Host "✓ PyInstaller: $pyinstallerVersion" -ForegroundColor Green
} catch {
    Write-Host "✗ PyInstaller 未安装: pip install pyinstaller" -ForegroundColor Red
    exit 1
}

# 清理旧的构建文件
Write-Host "▶ 清理旧构建产物..." -ForegroundColor Yellow
@("build", "dist", "*.egg-info", "__pycache__") | ForEach-Object {
    if (Test-Path $_) {
        Remove-Item -Path $_ -Recurse -Force -ErrorAction SilentlyContinue
        Write-Host "  ✓ 已删除: $_" -ForegroundColor Green
    }
}

# 安装/更新项目依赖
Write-Host "▶ 安装 Python 依赖..." -ForegroundColor Yellow
python -m pip install --upgrade pip setuptools wheel -q
python -m pip install -e . -q 2>&1 | Out-Null
Write-Host "✓ 依赖安装完成" -ForegroundColor Green

# 运行 PyInstaller
Write-Host "▶ 运行 PyInstaller..." -ForegroundColor Yellow
Write-Host "  规格文件: backend_trader.spec" -ForegroundColor Gray
Write-Host "  入口点: apps/trader/bundle_entry.py" -ForegroundColor Gray
Write-Host ""

pyinstaller backend_trader.spec --noconfirm --clean

if ($LASTEXITCODE -ne 0) {
    Write-Host "✗ PyInstaller 构建失败" -ForegroundColor Red
    exit 1
}

# 验证输出
Write-Host ""
if (Test-Path "dist/backend_trader.exe") {
    $fileSize = (Get-Item "dist/backend_trader.exe").Length / 1MB
    Write-Host "╔════════════════════════════════════════════════════════════════════════════╗" -ForegroundColor Green
    Write-Host "║ 构建成功! ✓                                                                  ║" -ForegroundColor Green
    Write-Host "╚════════════════════════════════════════════════════════════════════════════╝" -ForegroundColor Green
    Write-Host ""
    Write-Host "生成文件: dist/backend_trader.exe (${fileSize:F1} MB)" -ForegroundColor Green
    Write-Host ""
    Write-Host "运行方式:"
    Write-Host "  set TRADING_MODE=paper" -ForegroundColor Gray
    Write-Host "  dist\backend_trader.exe" -ForegroundColor Gray
} else {
    Write-Host "✗ backend_trader.exe 构建失败" -ForegroundColor Red
    exit 1
}
