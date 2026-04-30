# 🚀 AI Quant Trader - 构建快速参考

## 构建命令速查

### 一键完整构建
```powershell
cd d:\recording\AI_tool\AI-driven-cryptocurrency-quantitative-trading-system
powershell -ExecutionPolicy Bypass -File build_all.ps1
```
**输出**: `dist/backend_trader.exe` + `apps/desktop-client/release/AI Quant Trader Setup xxx.exe`

### 仅构建后端
```powershell
powershell -ExecutionPolicy Bypass -File build_backend.ps1
```
**输出**: `dist/backend_trader.exe` (~250 MB)

### 仅构建前端
```powershell
powershell -ExecutionPolicy Bypass -File build_frontend.ps1
```
**输出**: `apps/desktop-client/release/AI Quant Trader Setup xxx.exe` (~300 MB)

### 验证构建
```powershell
powershell -ExecutionPolicy Bypass -File verify_build.ps1
```

---

## 运行可执行程序

### 后端服务
```powershell
# 设置交易模式为纸面交易
set TRADING_MODE=paper

# 运行后端 API 服务
dist\backend_trader.exe

# 日志输出:
# 2026-04-30 14:30:45.123 | INFO     | Backend API started on http://localhost:8000
```

### 前端应用
```powershell
# 方式 1: 运行安装程序（推荐给用户）
.\apps\desktop-client\release\AI Quant Trader Setup 0.0.0.exe

# 方式 2: 直接运行可执行程序（开发用）
# .\apps\desktop-client\dist-electron\main.exe
```

---

## 故障排除速查

| 问题 | 解决方案 |
|------|--------|
| pip 错误 | `python -m pip cache purge && python -m ensurepip --upgrade` |
| npm 冲突 | `cd apps/desktop-client && npm install --legacy-peer-deps` |
| PyInstaller 失败 | `pyinstaller backend_trader.spec --noconfirm --clean` |
| electron-builder 失败 | `npm run build` (在 apps/desktop-client) |

---

## 文件位置速查

| 文件 | 位置 | 说明 |
|------|------|------|
| 后端可执行程序 | `dist/backend_trader.exe` | PyInstaller 生成 |
| 前端安装程序 | `apps/desktop-client/release/*.exe` | electron-builder 生成 |
| 后端源码 | `apps/trader/` | Python FastAPI 服务 |
| 前端源码 | `apps/desktop-client/src/` | React TypeScript 应用 |
| 配置文件 | `configs/` | 应用配置文件 |
| 构建脚本 | `build_*.ps1` | PowerShell 构建脚本 |

---

## 版本信息

```
构建日期: 2026-04-30
应用版本: 0.0.0

Python 环境:
  Python 3.9.13
  PyInstaller 6.19.0

Node.js 环境:
  Node.js v24.11.1
  npm 11.6.2
  Electron 29.x

构建工具:
  Vite 5.x
  TailwindCSS 4.x
  electron-builder latest
```

---

## 下一步

1. ✅ 执行构建命令
2. ✅ 等待编译完成（总时间 15-20 分钟）
3. ✅ 运行 `verify_build.ps1` 验证输出
4. ✅ 分发或安装应用

---

**需要帮助?** 查看 [BUILD_GUIDE.md](BUILD_GUIDE.md) 详细指南
