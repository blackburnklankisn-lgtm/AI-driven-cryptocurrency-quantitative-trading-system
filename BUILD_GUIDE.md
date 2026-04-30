# AI Quant Trader - 构建指南

## 📋 概览

本项目包含：
- **后端**: Python FastAPI 服务 + PyInstaller 可执行程序
- **前端**: Electron + React 桌面应用
- **安装程序**: NSIS Windows 安装程序

## 🔧 构建工具

### 后端构建（PyInstaller）
将 Python 源代码编译为独立的 Windows 可执行程序。

**规格文件**: `backend_trader.spec`
- 入口点: `apps/trader/bundle_entry.py`
- 输出: `dist/backend_trader.exe`
- 隐藏导入: google.generativeai, dotenv, ccxt 等

**构建时间**: 3-5 分钟
**输出大小**: 200-300 MB

### 前端构建（Vite + Electron）
编译 TypeScript/React，打包为 Electron 应用，生成安装程序。

**工具链**:
- Vite: 快速 React 构建
- electron-builder: 生成 NSIS 安装程序
- TailwindCSS: UI 样式

**构建时间**: 5-10 分钟
**输出大小**: 安装程序 200-400 MB

## 🚀 快速开始

### 方式 1: 完整自动构建（推荐）

```powershell
cd d:\recording\AI_tool\AI-driven-cryptocurrency-quantitative-trading-system
powershell -ExecutionPolicy Bypass -File build_all.ps1
```

**完成**: 生成后端 exe + 前端安装程序

### 方式 2: 仅构建后端

```powershell
powershell -ExecutionPolicy Bypass -File build_backend.ps1
```

**输出**: `dist/backend_trader.exe`

### 方式 3: 仅构建前端

```powershell
powershell -ExecutionPolicy Bypass -File build_frontend.ps1
```

**输出**: `apps/desktop-client/release/AI Quant Trader Setup xxx.exe`

## 📦 构建输出

### 后端
```
dist/
├── backend_trader.exe          ← 可执行程序
├── backend_trader.exe.manifest
└── ... (库文件)
```

**运行后端**:
```powershell
set TRADING_MODE=paper
dist\backend_trader.exe
```

### 前端
```
apps/desktop-client/
├── dist/                       ← Web 构建
│   └── index.html
├── dist-electron/              ← Electron 主进程
│   └── main.js
└── release/
    └── AI Quant Trader Setup 0.0.0.exe  ← 安装程序
```

**运行前端**:
```powershell
# 开发模式
npm run dev

# 生产可执行程序
.\release\AI Quant Trader Setup 0.0.0.exe
```

## 🔍 故障排除

### pip 元数据错误
```powershell
python -m pip cache purge
python -m ensurepip --upgrade
```

### npm 依赖冲突
```powershell
cd apps/desktop-client
rm -r node_modules package-lock.json
npm install --legacy-peer-deps
```

### PyInstaller 失败
```powershell
# 确保所有依赖已安装
python -m pip install -e .

# 检查规格文件
pyinstaller backend_trader.spec --noconfirm --clean
```

### Electron Builder 失败
```powershell
# 更新 electron-builder
npm install -g electron-builder@latest

# 重新生成
npm run build
```

## 📊 构建配置

### 后端 (pyproject.toml)
- Python >= 3.9
- 依赖: pandas, numpy, ccxt, sqlalchemy, fastapi, etc.
- PyInstaller 版本: 6.19.0

### 前端 (package.json)
- Node.js >= 18
- 框架: React 19, Vite 5, Electron 29
- 构建工具: electron-builder, TailwindCSS

## ✅ 验证构建

```powershell
# 检查后端
Test-Path dist/backend_trader.exe

# 检查前端
Get-ChildItem apps/desktop-client/release/*.exe

# 测试后端
set TRADING_MODE=paper
dist/backend_trader.exe --help

# 测试前端
.\apps\desktop-client\release\AI Quant Trader Setup*.exe
```

## 📝 版本信息

- **应用版本**: 0.0.0
- **构建日期**: 2026-04-30
- **Python**: 3.9.13
- **Node.js**: 24.11.1
- **PyInstaller**: 6.19.0

## 🎯 后续步骤

1. **分发**: 将 `AI Quant Trader Setup xxx.exe` 发给用户
2. **自动更新**: 实现增量更新机制
3. **代码签名**: 使用证书签名 exe 和安装程序
4. **测试**: 在干净的虚拟机上验证安装程序

## 📚 相关文档

- [QUICK_START.md](QUICK_START.md) - 用户快速启动指南
- [README.md](README.md) - 项目概览
- [pyproject.toml](pyproject.toml) - Python 项目配置
- [apps/desktop-client/package.json](apps/desktop-client/package.json) - 前端配置
