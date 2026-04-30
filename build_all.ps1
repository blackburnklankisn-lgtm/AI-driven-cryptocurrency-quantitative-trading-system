# ============================================================================
# AI Quant Trader - 完整构建脚本 (Windows PowerShell)
# ============================================================================
# 功能：
#   1. 清理旧的构建产物
#   2. 重新构建后端 Python 可执行程序 (PyInstaller)
#   3. 重新构建前端 Electron 应用
#   4. 生成安装程序 (NSIS)
#
# 用法: powershell -ExecutionPolicy Bypass -File build_all.ps1
# ============================================================================

param(
    [switch]$SkipBackend = $false,
    [switch]$SkipFrontend = $false,
    [switch]$SkipClean = $false
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

# ─────────────────────────────────────────────────────────────────────────
# 工具函数
# ─────────────────────────────────────────────────────────────────────────

function Write-Header {
    param([string]$Message)
    Write-Host ""
    Write-Host "╔" + ("═" * 76) + "╗" -ForegroundColor Cyan
    Write-Host "║ $Message" + (" " * (76 - $Message.Length)) + "║" -ForegroundColor Cyan
    Write-Host "╚" + ("═" * 76) + "╝" -ForegroundColor Cyan
    Write-Host ""
}

function Write-Step {
    param([string]$Message)
    Write-Host "▶ $Message" -ForegroundColor Green
}

function Write-Error-Custom {
    param([string]$Message)
    Write-Host "✗ 错误: $Message" -ForegroundColor Red
    exit 1
}

function Write-Success {
    param([string]$Message)
    Write-Host "✓ $Message" -ForegroundColor Green
}

# ─────────────────────────────────────────────────────────────────────────
# 环境检查
# ─────────────────────────────────────────────────────────────────────────

Write-Header "环境检查与依赖验证"

# 检查 Python
Write-Step "检查 Python 环境..."
try {
    $pythonVersion = python --version 2>&1
    Write-Success "找到 Python: $pythonVersion"
} catch {
    Write-Error-Custom "Python 未安装或不在 PATH 中"
}

# 检查 Node.js
Write-Step "检查 Node.js 环境..."
try {
    $nodeVersion = node --version
    $npmVersion = npm --version
    Write-Success "找到 Node.js: $nodeVersion"
    Write-Success "找到 npm: $npmVersion"
} catch {
    Write-Error-Custom "Node.js/npm 未安装或不在 PATH 中"
}

# 检查 PyInstaller
Write-Step "检查 PyInstaller..."
try {
    $pyinstallerVersion = pyinstaller --version
    Write-Success "找到 PyInstaller: $pyinstallerVersion"
} catch {
    Write-Error-Custom "PyInstaller 未安装。运行: pip install pyinstaller"
}

# ─────────────────────────────────────────────────────────────────────────
# 阶段 1: 清理旧构建产物
# ─────────────────────────────────────────────────────────────────────────

if (-not $SkipClean) {
    Write-Header "阶段 1: 清理旧构建产物"

    Write-Step "删除后端构建目录..."
    @("build", "dist", "*.egg-info") | ForEach-Object {
        if (Test-Path $_) {
            Remove-Item -Path $_ -Recurse -Force -ErrorAction SilentlyContinue
            Write-Success "已删除: $_"
        }
    }

    Write-Step "删除前端构建目录..."
    Push-Location "apps/desktop-client"
    @("dist", "dist-electron", "build/dist") | ForEach-Object {
        if (Test-Path $_) {
            Remove-Item -Path $_ -Recurse -Force -ErrorAction SilentlyContinue
            Write-Success "已删除: $_"
        }
    }
    Pop-Location

    Write-Success "清理完成"
} else {
    Write-Step "跳过清理步骤 (-SkipClean)"
}

# ─────────────────────────────────────────────────────────────────────────
# 阶段 2: 构建后端 (Python + PyInstaller)
# ─────────────────────────────────────────────────────────────────────────

if (-not $SkipBackend) {
    Write-Header "阶段 2: 构建后端可执行程序 (PyInstaller)"

    Write-Step "安装/更新 Python 依赖..."
    $env:PYTHONUNBUFFERED = "1"
    
    # 使用 pip 安装依赖
    python -m pip install --upgrade pip --quiet 2>&1 | Out-Null
    python -m pip install -e . --quiet 2>&1 | Out-Null
    Write-Success "依赖安装完成"

    Write-Step "运行 PyInstaller (backend_trader.spec)..."
    # 删除旧的构建产物
    if (Test-Path "build") {
        Remove-Item -Path "build" -Recurse -Force -ErrorAction SilentlyContinue
    }
    
    pyinstaller backend_trader.spec --noconfirm --clean 2>&1 | Tee-Object -Variable pyinstallerOutput | Out-Null
    
    if ($LASTEXITCODE -ne 0) {
        Write-Error-Custom "PyInstaller 构建失败"
    }

    # 验证输出文件
    if (Test-Path "dist/backend_trader.exe") {
        $exeSize = (Get-Item "dist/backend_trader.exe").Length / 1MB
        Write-Success "后端可执行程序已生成: dist/backend_trader.exe (约 ${exeSize:F1} MB)"
    } else {
        Write-Error-Custom "backend_trader.exe 构建失败"
    }

} else {
    Write-Step "跳过后端构建 (-SkipBackend)"
    if (-not (Test-Path "dist/backend_trader.exe")) {
        Write-Error-Custom "dist/backend_trader.exe 不存在且跳过了构建"
    }
}

# ─────────────────────────────────────────────────────────────────────────
# 阶段 3: 构建前端 (React + Electron)
# ─────────────────────────────────────────────────────────────────────────

if (-not $SkipFrontend) {
    Write-Header "阶段 3: 构建前端应用 (Electron + Vite)"

    Push-Location "apps/desktop-client"

    try {
        Write-Step "安装 npm 依赖 (可能需要 2-3 分钟)..."
        npm install --legacy-peer-deps 2>&1 | Out-Null
        Write-Success "npm 依赖安装完成"

        Write-Step "验证后端可执行程序文件..."
        $backendExe = "../../dist/backend_trader.exe"
        if (Test-Path $backendExe) {
            $exeSize = (Get-Item $backendExe).Length / 1MB
            Write-Success "后端程序已准备: $backendExe (约 ${exeSize:F1} MB)"
        } else {
            Write-Error-Custom "找不到后端可执行程序: $backendExe"
        }

        Write-Step "运行 npm run build (编译 TypeScript + Vite)..."
        npm run build 2>&1 | Tee-Object -Variable buildOutput | Out-Null
        
        if ($LASTEXITCODE -ne 0) {
            Write-Error-Custom "Vite 构建失败`n$buildOutput"
        }
        Write-Success "Vite 构建完成"

        Write-Step "编译 Electron 主进程 (TypeScript)..."
        npx tsc -p tsconfig.electron.json 2>&1 | Out-Null
        Write-Success "Electron 主进程编译完成"

        Write-Step "运行 electron-builder 生成安装程序..."
        npm run build 2>&1 | Tee-Object -Variable builderOutput | Out-Null
        
        if ($LASTEXITCODE -ne 0) {
            Write-Error-Custom "electron-builder 失败`n$builderOutput"
        }

        # 验证输出文件
        $setupExe = Get-ChildItem -Path "release" -Filter "*.exe" -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($setupExe) {
            $setupSize = $setupExe.Length / 1MB
            Write-Success "安装程序已生成: release/$($setupExe.Name) (约 ${setupSize:F1} MB)"
        } else {
            Write-Error-Custom "electron-builder 未生成 .exe 文件"
        }

        Write-Success "前端应用构建完成"

    } finally {
        Pop-Location
    }

} else {
    Write-Step "跳过前端构建 (-SkipFrontend)"
}

# ─────────────────────────────────────────────────────────────────────────
# 完成总结
# ─────────────────────────────────────────────────────────────────────────

Write-Header "构建完成总结"

Write-Host "后端可执行程序:" -ForegroundColor Yellow
if (Test-Path "dist/backend_trader.exe") {
    $exeSize = (Get-Item "dist/backend_trader.exe").Length / 1MB
    Write-Host "  ✓ dist/backend_trader.exe (~${exeSize:F1} MB)" -ForegroundColor Green
} else {
    Write-Host "  ✗ 未找到" -ForegroundColor Red
}

Write-Host ""
Write-Host "前端安装程序:" -ForegroundColor Yellow
$setupExe = Get-ChildItem -Path "apps/desktop-client/release" -Filter "*.exe" -ErrorAction SilentlyContinue | Select-Object -First 1
if ($setupExe) {
    $setupSize = $setupExe.Length / 1MB
    Write-Host "  ✓ apps/desktop-client/release/$($setupExe.Name) (~${setupSize:F1} MB)" -ForegroundColor Green
} else {
    Write-Host "  ✗ 未找到" -ForegroundColor Red
}

Write-Host ""
Write-Host "════════════════════════════════════════════════════════════════════════════" -ForegroundColor Cyan
Write-Host "🎉 所有构建任务完成！" -ForegroundColor Green
Write-Host "════════════════════════════════════════════════════════════════════════════" -ForegroundColor Cyan
Write-Host ""
Write-Host "下一步操作:" -ForegroundColor Yellow
Write-Host "  1. 用户端: 运行 $(if ($setupExe) { "apps/desktop-client/release/$($setupExe.Name)" } else { "[安装程序路径]" })"
Write-Host "  2. 开发者: 运行后端: dist/backend_trader.exe"
Write-Host ""
Write-Host "需要帮助? 查看 QUICK_START.md" -ForegroundColor Cyan
