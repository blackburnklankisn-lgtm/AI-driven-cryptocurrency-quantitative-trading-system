import { app, BrowserWindow, ipcMain } from 'electron';
import * as path from 'path';
import { spawn, ChildProcess } from 'child_process';
import * as http from 'http';

let mainWindow: BrowserWindow | null = null;
let pythonProcess: ChildProcess | null = null;
let backendLifecyclePromise: Promise<void> = Promise.resolve();

const isDev = process.env.NODE_ENV === 'development';
const gotSingleInstanceLock = app.requestSingleInstanceLock();
const BACKEND_HEALTH_PATH = '/api/v1/health';

function getBackendHealthUrls(): string[] {
  if (process.platform === 'win32') {
    return [`http://localhost:8000${BACKEND_HEALTH_PATH}`];
  }

  return [
    `http://localhost:8000${BACKEND_HEALTH_PATH}`,
    `http://127.0.0.1:8000${BACKEND_HEALTH_PATH}`,
  ];
}

// ── Python 后端进程管理 ──────────────────────────────────────

function findPythonExecutable(): string {
  // 生产模式：使用打包好的 backend_trader.exe
  if (!isDev) {
    const resourcesPath = process.resourcesPath;
    const candidate = path.join(resourcesPath, 'dist', 'backend_trader.exe');
    console.log(`[Electron] Looking for packed backend at: ${candidate}`);
    try {
      require('fs').accessSync(candidate);
      console.log('[Electron] Packed backend found ✅');
      return candidate;
    } catch (_) {
      console.error(`[Electron] ❌ Packed backend NOT found at: ${candidate}`);
    }
  }
  // 开发模式：使用系统 Python
  const exe = process.platform === 'win32' ? 'python' : 'python3';
  console.log(`[Electron] Dev mode: using system Python executable: ${exe}`);
  return exe;
}

function startPythonBackend(): void {
  if (isDev) {
    // 开发模式：假定用户已手动启动 Python 后端，不自动拉起
    console.log('[Electron] Dev mode: assuming Python backend is already running on localhost:8000.');
    console.log('[Electron] To start backend manually: $env:TRADING_MODE="paper"; python -m apps.trader.main');
    return;
  }

  const pythonExe = findPythonExecutable();
  
  // 生产模式下的项目根目录（用于 .env 和 状态 存取）
  // 默认为应用程序 exe 同级目录
  const execDir = path.dirname(app.getPath('exe'));
  const configPath = path.join(process.resourcesPath, 'configs', 'system.yaml');

  // 用户数据目录：%APPDATA%/AI Quant Trader/
  // 状态文件（trader_state.json）存放于此，确保卸载/升级不会丢失
  const userDataDir = path.join(app.getPath('appData'), 'AI Quant Trader');

  console.log(`[Electron] Backend exe   : ${pythonExe}`);
  console.log(`[Electron] Working dir   : ${execDir}`);
  console.log(`[Electron] CONFIG_PATH   : ${configPath}`);
  console.log(`[Electron] USER_DATA_DIR : ${userDataDir}`);
  console.log(`[Electron] TRADING_MODE  : paper`);

  const args = !isDev ? [] : ['-m', 'apps.trader.main']; 

  pythonProcess = spawn(pythonExe, args, {
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

  pythonProcess.stdout?.on('data', (data: Buffer) => {
    console.log(`[Python] ${data.toString().trim()}`);
  });

  pythonProcess.stderr?.on('data', (data: Buffer) => {
    console.error(`[Python ERR] ${data.toString().trim()}`);
  });

  pythonProcess.on('exit', (code) => {
    console.log(`[Electron] Python backend exited with code ${code}`);
    pythonProcess = null;
  });
}

function stopPythonBackend(reason = 'shutdown'): Promise<void> {
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
    } catch (error) {
      console.warn('[Electron] Failed to signal Python backend for shutdown:', error);
      finish();
      return;
    }

    setTimeout(() => {
      if (processToStop.exitCode === null) {
        try {
          processToStop.kill('SIGKILL');
        } catch (error) {
          console.warn('[Electron] Failed to force kill Python backend:', error);
        }
      }
      finish();
    }, 5000);
  });
}

function stopStalePythonBackends(reason: string): Promise<void> {
  if (isDev || process.platform !== 'win32') {
    return Promise.resolve();
  }

  console.log(`[Electron] Terminating stale backend_trader.exe processes before ${reason}...`);

  return new Promise((resolve) => {
    const killer = spawn('taskkill', ['/IM', 'backend_trader.exe', '/F', '/T'], {
      stdio: 'ignore',
      windowsHide: true,
    });

    killer.on('error', (error: Error) => {
      console.warn('[Electron] Failed to run taskkill for backend cleanup:', error.message);
      resolve();
    });

    killer.on('exit', (code) => {
      if (code === 0) {
        console.log('[Electron] Cleared stale backend_trader.exe processes.');
      } else {
        console.log(`[Electron] No stale backend_trader.exe processes needed cleanup (exit=${code ?? 'unknown'}).`);
      }
      resolve();
    });
  });
}

async function refreshPythonBackend(reason: string): Promise<void> {
  if (isDev) {
    startPythonBackend();
    return;
  }

  await stopPythonBackend(reason);
  await stopStalePythonBackends(reason);
  startPythonBackend();
  await waitForBackend(30, 1000);
}

function queueBackendRefresh(reason: string): Promise<void> {
  backendLifecyclePromise = backendLifecyclePromise
    .catch((error) => {
      console.error('[Electron] Previous backend lifecycle operation failed:', error);
    })
    .then(() => refreshPythonBackend(reason));
  return backendLifecyclePromise;
}

// ── 等待后端就绪（健康检查轮询） ─────────────────────────────

function waitForBackend(maxRetries = 30, intervalMs = 1000): Promise<void> {
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
        console.log(
          `[Electron] Waiting for backend readiness via ${healthUrls.join(', ')} ` +
          `(max ${maxRetries} checks, ${intervalMs}ms interval)...`,
        );
      } else if (attempts % 10 === 0) {
        const elapsedSeconds = ((Date.now() - startedAt) / 1000).toFixed(1);
        console.log(`[Electron] Backend still starting (${attempts}/${maxRetries}, ${elapsedSeconds}s elapsed)...`);
      }

      let attemptFinished = false;
      const finishAttempt = (next: () => void, detail?: string) => {
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
        } else {
          finishAttempt(retry, `${healthUrl} returned HTTP ${res.statusCode ?? 'unknown'}`);
        }
      });

      req.on('error', (err: Error) => {
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
        console.error(
          `[Electron] ❌ Backend did not start after ${attempts} checks ` +
          `(${elapsedSeconds}s). Last failure: ${lastFailureDetail}`,
        );
        reject(new Error(`Backend did not start in time: ${lastFailureDetail}`));
      } else {
        setTimeout(check, intervalMs);
      }
    };

    check();
  });
}

// ── 窗口创建 ─────────────────────────────────────────────────

const createWindow = () => {
  mainWindow = new BrowserWindow({
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
  } else {
    mainWindow.loadFile(path.join(__dirname, '../dist/index.html'));
  }

  mainWindow.on('closed', () => {
    mainWindow = null;
  });
};

async function createWindowWithFreshBackend(reason: string): Promise<void> {
  if (!isDev) {
    try {
      await queueBackendRefresh(reason);
    } catch (error) {
      console.error(`[Electron] Backend refresh failed during ${reason}:`, error);
    }
  }

  createWindow();
}

// ── 应用生命周期 ─────────────────────────────────────────────

if (!gotSingleInstanceLock) {
  app.quit();
} else {
  app.on('second-instance', () => {
    const primaryWindow = BrowserWindow.getAllWindows()[0] ?? null;
    if (primaryWindow) {
      if (primaryWindow.isMinimized()) {
        primaryWindow.restore();
      }
      primaryWindow.focus();
    }

    void createWindowWithFreshBackend('second-instance launch');
  });

  app.whenReady().then(async () => {
    await createWindowWithFreshBackend('initial launch');

    app.on('activate', () => {
      if (BrowserWindow.getAllWindows().length === 0) {
        void createWindowWithFreshBackend('app activate');
      }
    });
  });

  app.on('window-all-closed', () => {
    void stopPythonBackend('window-all-closed');
    if (process.platform !== 'darwin') {
      app.quit();
    }
  });

  app.on('before-quit', () => {
    void stopPythonBackend('before-quit');
  });

  // ── IPC: 前端可查询后端进程状态 ──────────────────────────────

  ipcMain.handle('get-backend-status', () => {
    return {
      running: pythonProcess !== null,
      pid: pythonProcess?.pid ?? null,
    };
  });
}
