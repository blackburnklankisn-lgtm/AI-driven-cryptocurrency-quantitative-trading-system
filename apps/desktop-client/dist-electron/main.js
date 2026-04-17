"use strict";
var __createBinding = (this && this.__createBinding) || (Object.create ? (function(o, m, k, k2) {
    if (k2 === undefined) k2 = k;
    var desc = Object.getOwnPropertyDescriptor(m, k);
    if (!desc || ("get" in desc ? !m.__esModule : desc.writable || desc.configurable)) {
      desc = { enumerable: true, get: function() { return m[k]; } };
    }
    Object.defineProperty(o, k2, desc);
}) : (function(o, m, k, k2) {
    if (k2 === undefined) k2 = k;
    o[k2] = m[k];
}));
var __setModuleDefault = (this && this.__setModuleDefault) || (Object.create ? (function(o, v) {
    Object.defineProperty(o, "default", { enumerable: true, value: v });
}) : function(o, v) {
    o["default"] = v;
});
var __importStar = (this && this.__importStar) || (function () {
    var ownKeys = function(o) {
        ownKeys = Object.getOwnPropertyNames || function (o) {
            var ar = [];
            for (var k in o) if (Object.prototype.hasOwnProperty.call(o, k)) ar[ar.length] = k;
            return ar;
        };
        return ownKeys(o);
    };
    return function (mod) {
        if (mod && mod.__esModule) return mod;
        var result = {};
        if (mod != null) for (var k = ownKeys(mod), i = 0; i < k.length; i++) if (k[i] !== "default") __createBinding(result, mod, k[i]);
        __setModuleDefault(result, mod);
        return result;
    };
})();
Object.defineProperty(exports, "__esModule", { value: true });
const electron_1 = require("electron");
const path = __importStar(require("path"));
const child_process_1 = require("child_process");
const http = __importStar(require("http"));
let mainWindow = null;
let pythonProcess = null;
const isDev = process.env.NODE_ENV === 'development';
// ── Python 后端进程管理 ──────────────────────────────────────
function findPythonExecutable() {
    // 生产模式：优先查找打包在 extraResources 中的 Python 可执行文件
    if (!isDev) {
        const resourcesPath = process.resourcesPath;
        const candidates = [
            path.join(resourcesPath, 'backend', 'trader.exe'), // PyInstaller 打包版
            path.join(resourcesPath, 'python', 'python.exe'), // 内嵌 Python
        ];
        for (const candidate of candidates) {
            try {
                require('fs').accessSync(candidate);
                return candidate;
            }
            catch (_) { }
        }
    }
    // 开发模式或回退：使用系统 Python
    return process.platform === 'win32' ? 'python' : 'python3';
}
function startPythonBackend() {
    if (isDev) {
        // 开发模式：假定用户已手动启动 Python 后端，不自动拉起
        console.log('[Electron] Dev mode: assuming Python backend is already running.');
        return;
    }
    const pythonExe = findPythonExecutable();
    const projectRoot = path.join(process.resourcesPath, 'backend');
    console.log(`[Electron] Starting Python backend: ${pythonExe}`);
    const args = pythonExe.endsWith('.exe') && !pythonExe.includes('python')
        ? [] // PyInstaller 打包版，直接运行
        : ['-m', 'apps.trader.main']; // 系统 Python，模块方式运行
    pythonProcess = (0, child_process_1.spawn)(pythonExe, args, {
        cwd: pythonExe.endsWith('.exe') && !pythonExe.includes('python')
            ? path.dirname(pythonExe)
            : projectRoot,
        env: {
            ...process.env,
            TRADING_MODE: 'paper',
            CONFIG_PATH: path.join(process.resourcesPath, 'configs', 'system.yaml'),
        },
        stdio: ['ignore', 'pipe', 'pipe'],
    });
    pythonProcess.stdout?.on('data', (data) => {
        console.log(`[Python] ${data.toString().trim()}`);
    });
    pythonProcess.stderr?.on('data', (data) => {
        console.error(`[Python ERR] ${data.toString().trim()}`);
    });
    pythonProcess.on('exit', (code) => {
        console.log(`[Electron] Python backend exited with code ${code}`);
        pythonProcess = null;
    });
}
function stopPythonBackend() {
    if (pythonProcess) {
        console.log('[Electron] Stopping Python backend...');
        pythonProcess.kill('SIGTERM');
        pythonProcess = null;
    }
}
// ── 等待后端就绪（健康检查轮询） ─────────────────────────────
function waitForBackend(maxRetries = 30, intervalMs = 1000) {
    return new Promise((resolve, reject) => {
        let attempts = 0;
        const check = () => {
            attempts++;
            const req = http.get('http://127.0.0.1:8000/api/v1/health', (res) => {
                if (res.statusCode === 200) {
                    console.log('[Electron] Backend is ready.');
                    resolve();
                }
                else {
                    retry();
                }
            });
            req.on('error', retry);
            req.setTimeout(500, () => { req.destroy(); retry(); });
        };
        const retry = () => {
            if (attempts >= maxRetries) {
                reject(new Error('Backend did not start in time'));
            }
            else {
                setTimeout(check, intervalMs);
            }
        };
        check();
    });
}
// ── 窗口创建 ─────────────────────────────────────────────────
const createWindow = () => {
    mainWindow = new electron_1.BrowserWindow({
        width: 1280,
        height: 800,
        title: 'AI Quant Trader',
        backgroundColor: '#0f172a',
        webPreferences: {
            preload: path.join(__dirname, 'preload.js'),
            nodeIntegration: false,
            contextIsolation: true,
        },
    });
    if (isDev) {
        mainWindow.loadURL('http://localhost:5173');
        mainWindow.webContents.openDevTools();
    }
    else {
        mainWindow.loadFile(path.join(__dirname, '../dist/index.html'));
    }
    mainWindow.on('closed', () => {
        mainWindow = null;
    });
};
// ── 应用生命周期 ─────────────────────────────────────────────
electron_1.app.whenReady().then(async () => {
    // 1. 启动 Python 后端（生产模式）
    startPythonBackend();
    // 2. 等待后端就绪后再创建窗口（生产模式等待，开发模式直接创建）
    if (!isDev) {
        try {
            await waitForBackend(30, 1000);
        }
        catch (err) {
            console.error('[Electron] Backend startup timeout:', err);
            // 即使后端未就绪也打开窗口，前端会显示 Connecting... 状态
        }
    }
    createWindow();
    electron_1.app.on('activate', () => {
        if (electron_1.BrowserWindow.getAllWindows().length === 0) {
            createWindow();
        }
    });
});
electron_1.app.on('window-all-closed', () => {
    stopPythonBackend();
    if (process.platform !== 'darwin') {
        electron_1.app.quit();
    }
});
electron_1.app.on('before-quit', () => {
    stopPythonBackend();
});
// ── IPC: 前端可查询后端进程状态 ──────────────────────────────
electron_1.ipcMain.handle('get-backend-status', () => {
    return {
        running: pythonProcess !== null,
        pid: pythonProcess?.pid ?? null,
    };
});
