import { app, BrowserWindow, ipcMain } from 'electron';
import * as path from 'path';
import { spawn, ChildProcess } from 'child_process';
import * as http from 'http';

let mainWindow: BrowserWindow | null = null;
let pythonProcess: ChildProcess | null = null;

const isDev = process.env.NODE_ENV === 'development';

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

function stopPythonBackend(): void {
  if (pythonProcess) {
    console.log('[Electron] Stopping Python backend...');
    pythonProcess.kill('SIGTERM');
    pythonProcess = null;
  }
}

// ── 等待后端就绪（健康检查轮询） ─────────────────────────────

function waitForBackend(maxRetries = 30, intervalMs = 1000): Promise<void> {
  return new Promise((resolve, reject) => {
    let attempts = 0;
    const healthUrls = ['http://localhost:8000/api/v1/health', 'http://127.0.0.1:8000/api/v1/health'];
    const check = () => {
      attempts++;
      const healthUrl = healthUrls[(attempts - 1) % healthUrls.length];
      console.log(`[Electron] Health check #${attempts}/${maxRetries} → GET ${healthUrl}`);
      const req = http.get(healthUrl, (res) => {
        console.log(`[Electron] Health check response: HTTP ${res.statusCode}`);
        if (res.statusCode === 200) {
          console.log('[Electron] ✅ Backend is ready.');
          resolve();
        } else {
          retry();
        }
      });
      req.on('error', (err: Error) => {
        console.warn(`[Electron] Health check #${attempts} error: ${err.message}`);
        retry();
      });
      req.setTimeout(500, () => { req.destroy(); retry(); });
    };
    const retry = () => {
      if (attempts >= maxRetries) {
        console.error(`[Electron] ❌ Backend did not start after ${maxRetries} attempts.`);
        reject(new Error('Backend did not start in time'));
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

// ── 应用生命周期 ─────────────────────────────────────────────

app.whenReady().then(async () => {
  // 1. 启动 Python 后端（生产模式）
  startPythonBackend();

  // 2. 等待后端就绪后再创建窗口（生产模式等待，开发模式直接创建）
  if (!isDev) {
    try {
      await waitForBackend(30, 1000);
    } catch (err) {
      console.error('[Electron] Backend startup timeout:', err);
      // 即使后端未就绪也打开窗口，前端会显示 Connecting... 状态
    }
  }

  createWindow();

  app.on('activate', () => {
    if (BrowserWindow.getAllWindows().length === 0) {
      createWindow();
    }
  });
});

app.on('window-all-closed', () => {
  stopPythonBackend();
  if (process.platform !== 'darwin') {
    app.quit();
  }
});

app.on('before-quit', () => {
  stopPythonBackend();
});

// ── IPC: 前端可查询后端进程状态 ──────────────────────────────

ipcMain.handle('get-backend-status', () => {
  return {
    running: pythonProcess !== null,
    pid: pythonProcess?.pid ?? null,
  };
});
