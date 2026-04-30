# ============================================================================
# AI Quant Trader - 前端构建脚本 (快速模式)
# ============================================================================
# 功能: 仅构建前端 Electron 应用及安装程序
# 用法: powershell -ExecutionPolicy Bypass -File build_frontend.ps1
# ============================================================================

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

Write-Host "╔════════════════════════════════════════════════════════════════════════════╗" -ForegroundColor Cyan
Write-Host "║                前端应用构建 (Vite + Electron Builder)                      ║" -ForegroundColor Cyan
Write-Host "╚════════════════════════════════════════════════════════════════════════════╝" -ForegroundColor Cyan
Write-Host ""

# 检查环境
Write-Host "▶ 环境检查..." -ForegroundColor Yellow
try {
    $nodeVersion = node --version
    $npmVersion = npm --version
    Write-Host "✓ Node.js: $nodeVersion" -ForegroundColor Green
    Write-Host "✓ npm: $npmVersion" -ForegroundColor Green
} catch {
    Write-Host "✗ Node.js/npm 未安装" -ForegroundColor Red
    exit 1
}

Push-Location "apps/desktop-client"

try {
    # 检查后端文件
    Write-Host "▶ 验证后端可执行程序..." -ForegroundColor Yellow
    $backendExe = "../../dist/backend_trader.exe"
    if (Test-Path $backendExe) {
        $exeSize = (Get-Item $backendExe).Length / 1MB
        Write-Host "✓ 后端程序已准备: dist/backend_trader.exe (${exeSize:F1} MB)" -ForegroundColor Green
    } else {
        Write-Host "⚠ 警告: 找不到后端程序 $backendExe" -ForegroundColor Yellow
        Write-Host "        请先运行: powershell -ExecutionPolicy Bypass -File ../build_backend.ps1" -ForegroundColor Yellow
        Write-Host "        继续进行...但最终安装程序可能不完整" -ForegroundColor Yellow
    }

    # 清理旧文件
    Write-Host "▶ 清理旧构建产物..." -ForegroundColor Yellow
    @("dist", "dist-electron") | ForEach-Object {
        if (Test-Path $_) {
            Remove-Item -Path $_ -Recurse -Force -ErrorAction SilentlyContinue
            Write-Host "  ✓ 已删除: $_" -ForegroundColor Green
        }
    }

    # 安装 npm 依赖
    Write-Host "▶ 安装 npm 依赖 (首次可能需要 2-3 分钟)..." -ForegroundColor Yellow
    npm install --legacy-peer-deps 2>&1 | Out-Null
    Write-Host "✓ npm 依赖已安装" -ForegroundColor Green

    # TypeScript 编译 (Web)
    Write-Host "▶ 编译 React 前端 (TypeScript)..." -ForegroundColor Yellow
    npx tsc -b 2>&1 | Out-Null
    Write-Host "✓ TypeScript 编译完成" -ForegroundColor Green

    # Vite 构建
    Write-Host "▶ 运行 Vite 构建..." -ForegroundColor Yellow
    npm run build 2>&1 | Out-Null
    if ($LASTEXITCODE -ne 0) {
        Write-Host "✗ Vite 构建失败" -ForegroundColor Red
        exit 1
    }
    Write-Host "✓ Vite 构建完成" -ForegroundColor Green

    # 编译 Electron 主进程
    Write-Host "▶ 编译 Electron 主进程..." -ForegroundColor Yellow
    npx tsc -p tsconfig.electron.json 2>&1 | Out-Null
    Write-Host "✓ Electron 主进程编译完成" -ForegroundColor Green

    # Electron Builder
    Write-Host "▶ 运行 electron-builder (生成安装程序)..." -ForegroundColor Yellow
    Write-Host "  这可能需要 2-5 分钟..." -ForegroundColor Gray
    npm run build 2>&1 | Out-Null
    if ($LASTEXITCODE -ne 0) {
        Write-Host "✗ electron-builder 失败" -ForegroundColor Red
        exit 1
    }

    # 验证输出
    Write-Host ""
    $setupExe = Get-ChildItem -Path "release" -Filter "*.exe" -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($setupExe) {
        $setupSize = $setupExe.Length / 1MB
        Write-Host "╔════════════════════════════════════════════════════════════════════════════╗" -ForegroundColor Green
        Write-Host "║ 构建成功! ✓                                                                  ║" -ForegroundColor Green
        Write-Host "╚════════════════════════════════════════════════════════════════════════════╝" -ForegroundColor Green
        Write-Host ""
        Write-Host "生成文件: release/$($setupExe.Name) (${setupSize:F1} MB)" -ForegroundColor Green
        Write-Host ""
        Write-Host "运行安装程序:"
        Write-Host "  .\release\$($setupExe.Name)" -ForegroundColor Gray
    } else {
        Write-Host "✗ 安装程序生成失败" -ForegroundColor Red
        exit 1
    }

} finally {
    Pop-Location
}
