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
let backendLifecyclePromise = Promise.resolve();
const isDev = process.env.NODE_ENV === 'development';
const gotSingleInstanceLock = electron_1.app.requestSingleInstanceLock();
const BACKEND_HEALTH_PATH = '/api/v1/health';
function getBackendHealthUrls() {
    if (process.platform === 'win32') {
        return [`http://localhost:8000${BACKEND_HEALTH_PATH}`];
    }
    return [
        `http://localhost:8000${BACKEND_HEALTH_PATH}`,
        `http://127.0.0.1:8000${BACKEND_HEALTH_PATH}`,
    ];
}
// ── Python 后端进程管理 ──────────────────────────────────────
function findPythonExecutable() {
    // 生产模式：使用打包好的 backend_trader.exe
    if (!isDev) {
        const resourcesPath = process.resourcesPath;
        const candidate = path.join(resourcesPath, 'dist', 'backend_trader.exe');
        console.log(`[Electron] Looking for packed backend at: ${candidate}`);
        try {
            require('fs').accessSync(candidate);
            console.log('[Electron] Packed backend found ✅');
            return candidate;
        }
        catch (_) {
            console.error(`[Electron] ❌ Packed backend NOT found at: ${candidate}`);
        }
    }
    // 开发模式：使用系统 Python
    const exe = process.platform === 'win32' ? 'python' : 'python3';
    console.log(`[Electron] Dev mode: using system Python executable: ${exe}`);
    return exe;
}
function startPythonBackend() {
    if (isDev) {
        // 开发模式：假定用户已手动启动 Python 后端，不自动拉起
        console.log('[Electron] Dev mode: assuming Python backend is already running on localhost:8000.');
        console.log('[Electron] To start backend manually: $env:TRADING_MODE="paper"; python -m apps.trader.main');
        return;
    }
    const pythonExe = findPythonExecutable();
    // 生产模式下的项目根目录（用于 .env 和 状态 存取）
    // 默认为应用程序 exe 同级目录
    const execDir = path.dirname(electron_1.app.getPath('exe'));
    const configPath = path.join(process.resourcesPath, 'configs', 'system.yaml');
    // 用户数据目录：%APPDATA%/AI Quant Trader/
    // 状态文件（trader_state.json）存放于此，确保卸载/升级不会丢失
    const userDataDir = path.join(electron_1.app.getPath('appData'), 'AI Quant Trader');
    console.log(`[Electron] Backend exe   : ${pythonExe}`);
    console.log(`[Electron] Working dir   : ${execDir}`);
    console.log(`[Electron] CONFIG_PATH   : ${configPath}`);
    console.log(`[Electron] USER_DATA_DIR : ${userDataDir}`);
    console.log(`[Electron] TRADING_MODE  : paper`);
    const args = !isDev ? [] : ['-m', 'apps.trader.main'];
    pythonProcess = (0, child_process_1.spawn)(pythonExe, args, {
        cwd: execDir,
        env: {
            ...process.env,
            TRADING_MODE: 'paper',
            CONFIG_PATH: configPath,
            USER_DATA_DIR: userDataDir,
        },
        stdio: ['ignore', 'pipe', 'pipe'],
    });
    console.log(`[Electron] Python process started, PID: ${pythonProcess.pid}`);
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
function stopPythonBackend(reason = 'shutdown') {
    if (!pythonProcess) {
        return Promise.resolve();
    }
    const processToStop = pythonProcess;
    pythonProcess = null;
    console.log(`[Electron] Stopping Python backend (${reason}), PID: ${processToStop.pid ?? 'unknown'}...`);
    return new Promise((resolve) => {
        let settled = false;
        const finish = () => {
            if (!settled) {
                settled = true;
                resolve();
            }
        };
        processToStop.once('exit', () => finish());
        try {
            processToStop.kill('SIGTERM');
        }
        catch (error) {
            console.warn('[Electron] Failed to signal Python backend for shutdown:', error);
            finish();
            return;
        }
        setTimeout(() => {
            if (processToStop.exitCode === null) {
                try {
                    processToStop.kill('SIGKILL');
                }
                catch (error) {
                    console.warn('[Electron] Failed to force kill Python backend:', error);
                }
            }
            finish();
        }, 5000);
    });
}
function stopStalePythonBackends(reason) {
    if (isDev || process.platform !== 'win32') {
        return Promise.resolve();
    }
    console.log(`[Electron] Terminating stale backend_trader.exe processes before ${reason}...`);
    return new Promise((resolve) => {
        const killer = (0, child_process_1.spawn)('taskkill', ['/IM', 'backend_trader.exe', '/F', '/T'], {
            stdio: 'ignore',
            windowsHide: true,
        });
        killer.on('error', (error) => {
            console.warn('[Electron] Failed to run taskkill for backend cleanup:', error.message);
            resolve();
        });
        killer.on('exit', (code) => {
            if (code === 0) {
                console.log('[Electron] Cleared stale backend_trader.exe processes.');
            }
            else {
                console.log(`[Electron] No stale backend_trader.exe processes needed cleanup (exit=${code ?? 'unknown'}).`);
            }
            resolve();
        });
    });
}
async function refreshPythonBackend(reason) {
    if (isDev) {
        startPythonBackend();
        return;
    }
    await stopPythonBackend(reason);
    await stopStalePythonBackends(reason);
    startPythonBackend();
    await waitForBackend(30, 1000);
}
function queueBackendRefresh(reason) {
    backendLifecyclePromise = backendLifecyclePromise
        .catch((error) => {
        console.error('[Electron] Previous backend lifecycle operation failed:', error);
    })
        .then(() => refreshPythonBackend(reason));
    return backendLifecyclePromise;
}
// ── 等待后端就绪（健康检查轮询） ─────────────────────────────
function waitForBackend(maxRetries = 30, intervalMs = 1000) {
    return new Promise((resolve, reject) => {
        let attempts = 0;
        const healthUrls = getBackendHealthUrls();
        const startedAt = Date.now();
        const requestTimeoutMs = process.platform === 'win32' ? 1000 : 500;
        let lastFailureDetail = 'no response received';
        const check = () => {
            attempts++;
            const healthUrl = healthUrls[(attempts - 1) % healthUrls.length];
            if (attempts === 1) {
                console.log(`[Electron] Waiting for backend readiness via ${healthUrls.join(', ')} ` +
                    `(max ${maxRetries} checks, ${intervalMs}ms interval)...`);
            }
            else if (attempts % 10 === 0) {
                const elapsedSeconds = ((Date.now() - startedAt) / 1000).toFixed(1);
                console.log(`[Electron] Backend still starting (${attempts}/${maxRetries}, ${elapsedSeconds}s elapsed)...`);
            }
            let attemptFinished = false;
            const finishAttempt = (next, detail) => {
                if (attemptFinished) {
                    return;
                }
                attemptFinished = true;
                if (detail) {
                    lastFailureDetail = detail;
                }
                next();
            };
            const req = http.get(healthUrl, (res) => {
                res.resume();
                if (res.statusCode === 200) {
                    const elapsedSeconds = ((Date.now() - startedAt) / 1000).toFixed(1);
                    finishAttempt(() => {
                        console.log(`[Electron] ✅ Backend is ready after ${attempts} checks (${elapsedSeconds}s).`);
                        resolve();
                    });
                }
                else {
                    finishAttempt(retry, `${healthUrl} returned HTTP ${res.statusCode ?? 'unknown'}`);
                }
            });
            req.on('error', (err) => {
                finishAttempt(retry, `${healthUrl} error: ${err.message}`);
            });
            req.setTimeout(requestTimeoutMs, () => {
                finishAttempt(retry, `${healthUrl} timed out after ${requestTimeoutMs}ms`);
                req.destroy();
            });
        };
        const retry = () => {
            if (attempts >= maxRetries) {
                const elapsedSeconds = ((Date.now() - startedAt) / 1000).toFixed(1);
                console.error(`[Electron] ❌ Backend did not start after ${attempts} checks ` +
                    `(${elapsedSeconds}s). Last failure: ${lastFailureDetail}`);
                reject(new Error(`Backend did not start in time: ${lastFailureDetail}`));
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
async function createWindowWithFreshBackend(reason) {
    if (!isDev) {
        try {
            await queueBackendRefresh(reason);
        }
        catch (error) {
            console.error(`[Electron] Backend refresh failed during ${reason}:`, error);
        }
    }
    createWindow();
}
// ── 应用生命周期 ─────────────────────────────────────────────
if (!gotSingleInstanceLock) {
    electron_1.app.quit();
}
else {
    electron_1.app.on('second-instance', () => {
        const primaryWindow = electron_1.BrowserWindow.getAllWindows()[0] ?? null;
        if (primaryWindow) {
            if (primaryWindow.isMinimized()) {
                primaryWindow.restore();
            }
            primaryWindow.focus();
        }
        void createWindowWithFreshBackend('second-instance launch');
    });
    electron_1.app.whenReady().then(async () => {
        await createWindowWithFreshBackend('initial launch');
        electron_1.app.on('activate', () => {
            if (electron_1.BrowserWindow.getAllWindows().length === 0) {
                void createWindowWithFreshBackend('app activate');
            }
        });
    });
    electron_1.app.on('window-all-closed', () => {
        void stopPythonBackend('window-all-closed');
        if (process.platform !== 'darwin') {
            electron_1.app.quit();
        }
    });
    electron_1.app.on('before-quit', () => {
        void stopPythonBackend('before-quit');
    });
    // ── IPC: 前端可查询后端进程状态 ──────────────────────────────
    electron_1.ipcMain.handle('get-backend-status', () => {
        return {
            running: pythonProcess !== null,
            pid: pythonProcess?.pid ?? null,
        };
    });
}
