# ============================================================================
# AI Quant Trader - 构建验证脚本
# ============================================================================
# 功能: 检查构建输出文件
# 用法: powershell -ExecutionPolicy Bypass -File verify_build.ps1
# ============================================================================

Set-StrictMode -Version Latest

Write-Host "╔════════════════════════════════════════════════════════════════════════════╗" -ForegroundColor Cyan
Write-Host "║                    构建输出文件检查                                        ║" -ForegroundColor Cyan
Write-Host "╚════════════════════════════════════════════════════════════════════════════╝" -ForegroundColor Cyan
Write-Host ""

$backendOk = $false
$frontendOk = $false

# ─────────────────────────────────────────────────────────────────────────
# 检查后端
# ─────────────────────────────────────────────────────────────────────────
Write-Host "▶ 后端检查" -ForegroundColor Yellow

$backendExe = "dist\backend_trader.exe"
if (Test-Path $backendExe) {
    $exeSize = (Get-Item $backendExe).Length / 1MB
    $exeTime = (Get-Item $backendExe).LastWriteTime
    Write-Host "  ✓ 文件: $backendExe" -ForegroundColor Green
    Write-Host "    大小: ${exeSize:F1} MB" -ForegroundColor Green
    Write-Host "    修改: $exeTime" -ForegroundColor Green
    
    # 验证文件有效性
    if ((Get-Item $backendExe).Length -gt 50MB) {
        Write-Host "    状态: 有效 (>50MB)" -ForegroundColor Green
        $backendOk = $true
    } else {
        Write-Host "    状态: 可能不完整 (<50MB)" -ForegroundColor Yellow
    }
} else {
    Write-Host "  ✗ 未找到: $backendExe" -ForegroundColor Red
    Write-Host "    请运行: powershell -ExecutionPolicy Bypass -File build_backend.ps1" -ForegroundColor Yellow
}

Write-Host ""

# ─────────────────────────────────────────────────────────────────────────
# 检查前端
# ─────────────────────────────────────────────────────────────────────────
Write-Host "▶ 前端检查" -ForegroundColor Yellow

$setupExe = Get-ChildItem -Path "apps\desktop-client\release" -Filter "*.exe" -ErrorAction SilentlyContinue | Select-Object -First 1

if ($setupExe) {
    $setupSize = $setupExe.Length / 1MB
    $setupTime = $setupExe.LastWriteTime
    Write-Host "  ✓ 文件: apps/desktop-client/release/$($setupExe.Name)" -ForegroundColor Green
    Write-Host "    大小: ${setupSize:F1} MB" -ForegroundColor Green
    Write-Host "    修改: $setupTime" -ForegroundColor Green
    
    # 验证文件有效性
    if ($setupExe.Length -gt 100MB) {
        Write-Host "    状态: 有效 (>100MB)" -ForegroundColor Green
        $frontendOk = $true
    } else {
        Write-Host "    状态: 可能不完整 (<100MB)" -ForegroundColor Yellow
    }
} else {
    Write-Host "  ✗ 未找到安装程序" -ForegroundColor Red
    Write-Host "    请运行: powershell -ExecutionPolicy Bypass -File build_frontend.ps1" -ForegroundColor Yellow
}

Write-Host ""

# ─────────────────────────────────────────────────────────────────────────
# 检查辅助文件
# ─────────────────────────────────────────────────────────────────────────
Write-Host "▶ 辅助文件检查" -ForegroundColor Yellow

@(
    @("后端脚本", "build_backend.ps1"),
    @("前端脚本", "build_frontend.ps1"),
    @("完整脚本", "build_all.ps1"),
    @("配置文件", "pyproject.toml"),
    @("前端配置", "apps\desktop-client\package.json"),
    @("PyInstaller 规格", "backend_trader.spec")
) | ForEach-Object {
    $name, $path = $_
    if (Test-Path $path) {
        Write-Host "  ✓ $name" -ForegroundColor Green
    } else {
        Write-Host "  ✗ $name (缺失)" -ForegroundColor Red
    }
}

Write-Host ""

# ─────────────────────────────────────────────────────────────────────────
# 总结
# ─────────────────────────────────────────────────────────────────────────

if ($backendOk -and $frontendOk) {
    Write-Host "╔════════════════════════════════════════════════════════════════════════════╗" -ForegroundColor Green
    Write-Host "║ ✓ 所有构建文件都已准备就绪！                                              ║" -ForegroundColor Green
    Write-Host "╚════════════════════════════════════════════════════════════════════════════╝" -ForegroundColor Green
    Write-Host ""
    Write-Host "可以进行以下操作:" -ForegroundColor Yellow
    Write-Host "  1. 分发: apps/desktop-client/release/$($setupExe.Name)" -ForegroundColor Gray
    Write-Host "  2. 运行后端: dist\backend_trader.exe" -ForegroundColor Gray
    Write-Host "  3. 运行前端: apps\desktop-client\release\$($setupExe.Name)" -ForegroundColor Gray
} elseif ($backendOk -or $frontendOk) {
    Write-Host "╔════════════════════════════════════════════════════════════════════════════╗" -ForegroundColor Yellow
    Write-Host "║ ⚠ 部分构建完成，还需要继续构建                                            ║" -ForegroundColor Yellow
    Write-Host "╚════════════════════════════════════════════════════════════════════════════╝" -ForegroundColor Yellow
    if (-not $backendOk) {
        Write-Host ""
        Write-Host "构建后端: powershell -ExecutionPolicy Bypass -File build_backend.ps1" -ForegroundColor Yellow
    }
    if (-not $frontendOk) {
        Write-Host ""
        Write-Host "构建前端: powershell -ExecutionPolicy Bypass -File build_frontend.ps1" -ForegroundColor Yellow
    }
} else {
    Write-Host "╔════════════════════════════════════════════════════════════════════════════╗" -ForegroundColor Red
    Write-Host "║ ✗ 还未执行任何构建                                                        ║" -ForegroundColor Red
    Write-Host "╚════════════════════════════════════════════════════════════════════════════╝" -ForegroundColor Red
    Write-Host ""
    Write-Host "执行完整构建:" -ForegroundColor Yellow
    Write-Host "  powershell -ExecutionPolicy Bypass -File build_all.ps1" -ForegroundColor Yellow
}

Write-Host ""
