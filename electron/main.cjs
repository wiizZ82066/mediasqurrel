// Media Squirrel — Electron 主进程
// 职责: 启动/关闭 Python 后端、健康检查、主窗口、系统托盘。
// 绝不使用外部浏览器（无 webbrowser.open / shell.openExternal 打开主界面）。
'use strict';

const { app, BrowserWindow, Tray, Menu, nativeImage, dialog, ipcMain } = require('electron');
const { spawn } = require('child_process');
const http = require('http');
const net = require('net');
const path = require('path');
const fs = require('fs');
const { randomBytes } = require('crypto');
const desktopToken = randomBytes(32).toString('hex');

const IS_DEV = !!process.env.DEV_SERVER_URL;
const DEV_URL = process.env.DEV_SERVER_URL || '';

// ---------- 路径 ----------
function backendExePath() {
  // 打包后: resources/backend/MediaSquirrelBackend/MediaSquirrelBackend.exe
  const packaged = path.join(process.resourcesPath, 'backend', 'MediaSquirrelBackend', 'MediaSquirrelBackend.exe');
  if (fs.existsSync(packaged)) return packaged;
  // 开发模式: backend-dist/MediaSquirrelBackend/MediaSquirrelBackend.exe（若已打包）
  const dev = path.join(__dirname, '..', 'backend-dist', 'MediaSquirrelBackend', 'MediaSquirrelBackend.exe');
  if (fs.existsSync(dev)) return dev;
  return null; // 开发模式未打包 -> 用 python 源码跑
}

function userDataDir() {
  const local = process.env.LOCALAPPDATA || path.join(process.env.USERPROFILE || '~', 'AppData', 'Local');
  return process.env.MS_DATA_DIR || path.join(local, 'Media Squirrel');
}

// ---------- 端口 ----------
function pickFreePort() {
  return new Promise((resolve, reject) => {
    const srv = net.createServer();
    srv.listen(0, '127.0.0.1', () => {
      const port = srv.address().port;
      srv.close(() => resolve(port));
    });
    srv.on('error', reject);
  });
}

// ---------- 健康检查 ----------
function waitForBackend(port, timeoutMs = 60000) {
  const started = Date.now();
  return new Promise((resolve, reject) => {
    (function probe() {
      const req = http.get({ host: '127.0.0.1', port, path: '/api/scripts', timeout: 2000 }, (res) => {
        res.resume();
        if (res.statusCode === 200) resolve();
        else retry();
      });
      req.on('error', retry);
      req.on('timeout', () => { req.destroy(); retry(); });
      function retry() {
        if (Date.now() - started > timeoutMs) {
          reject(new Error(`后端 ${timeoutMs / 1000}s 内未就绪`));
        } else {
          setTimeout(probe, 500);
        }
      }
    })();
  });
}

// ---------- Python 后端 ----------
let backendProc = null;
let currentPort = 0;

async function startBackend(port) {
  currentPort = port;
  const exe = backendExePath();
  const dataDir = userDataDir();
  fs.mkdirSync(dataDir, { recursive: true });

  // Playwright 浏览器目录：优先随包分发的 chromium，其次用户已装的 ms-playwright
  const env = { ...process.env, MS_PORT: String(port), MS_DESKTOP_TOKEN: desktopToken };
  const bundledBrowsers = path.join(process.resourcesPath || '', 'playwright-browsers');
  if (!IS_DEV && fs.existsSync(bundledBrowsers)) {
    env.PLAYWRIGHT_BROWSERS_PATH = bundledBrowsers;
  }

  if (exe) {
    backendProc = spawn(exe, ['--no-browser', '--no-tray', '--port', String(port)], {
      cwd: dataDir,
      env,
      windowsHide: true,
      stdio: ['pipe', 'pipe', 'pipe'], // stdin 保持连接：backend 看门狗靠 stdin EOF 自杀
    });
  } else if (IS_DEV) {
    // 开发模式：python 源码
    const root = path.join(__dirname, '..');
    backendProc = spawn('python', ['run.py', '--no-browser', '--no-tray', '--port', String(port)], {
      cwd: root, env, windowsHide: true,
      stdio: ['pipe', 'pipe', 'pipe'],
    });
  } else {
    throw new Error('未找到 MediaSquirrelBackend.exe');
  }

  backendProc.stdout.on('data', (d) => process.stdout.write(`[backend] ${d}`));
  backendProc.stderr.on('data', (d) => process.stderr.write(`[backend] ${d}`));
  const startedProc = backendProc;
  backendProc.on('exit', code => {
    if (backendProc === startedProc) onBackendUnexpectedExit(code);
  });
}

// backend 意外退出：自动重启一次；正常退出（quitting）忽略
let backendRestarted = false;
function onBackendUnexpectedExit(code) {
  console.log(`[backend] 退出 code=${code}`);
  backendProc = null;
  if (app.isQuitting || code === 0) return;

  if (!backendRestarted) {
    backendRestarted = true;
    console.log('[backend] 意外退出，尝试重启…');
    startBackend(currentPort)
      .then(() => waitForBackend(currentPort, 30000))
      .then(() => {
        console.log('[backend] 重启成功');
        if (mainWindow) mainWindow.webContents.reload();
      })
      .catch(() => showBackendDeadDialog());
  } else {
    showBackendDeadDialog();
  }
}

function showBackendDeadDialog() {
  dialog.showErrorBox(
    'Media Squirrel 后端服务异常',
    '本地后端服务已停止且自动恢复失败。\n请重新启动应用；若反复出现请重新安装。'
  );
  app.quit();
}

let stopping = null;
function killBackend() {
  if (stopping) return stopping;
  if (!backendProc) return Promise.resolve();
  const proc = backendProc;
  // Detach unexpected-exit recovery during an intentional shutdown.
  backendProc = null;
  stopping = new Promise((resolve, reject) => {
    const killer = spawn('taskkill', ['/PID', String(proc.pid), '/T', '/F'], { windowsHide: true, stdio: 'ignore' });
    const timer = setTimeout(() => reject(new Error('后端关闭超时，已取消更新安装')), 8000);
    killer.once('error', error => { clearTimeout(timer); reject(error); });
    killer.once('exit', code => {
      clearTimeout(timer);
      if (code === 0 || proc.exitCode !== null) resolve();
      else reject(new Error('无法关闭后端，已取消更新安装'));
    });
  }).catch(error => {
    if (proc.exitCode === null) backendProc = proc;
    throw error;
  }).finally(() => { stopping = null; });
  return stopping;
}

// ---------- 窗口与托盘 ----------
let mainWindow = null;
let tray = null;

function createWindow(url) {
  mainWindow = new BrowserWindow({
    width: 1360,
    height: 900,
    minWidth: 980,
    minHeight: 640,
    show: false, // 健康检查通过、页面就绪后再显示
    icon: path.join(__dirname, 'build', 'icon.ico'),
    autoHideMenuBar: true,
    backgroundColor: '#f5f5f7',
    webPreferences: {
      preload: path.join(__dirname, 'preload.cjs'),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
    },
  });

  mainWindow.once('ready-to-show', () => mainWindow.show());
  mainWindow.loadURL(url);
  // The update bridge belongs only to this local app, never remote documents.
  const origin = new URL(url).origin;
  mainWindow.webContents.on('will-navigate', (event, target) => {
    if (new URL(target).origin !== origin) event.preventDefault();
  });
  mainWindow.webContents.setWindowOpenHandler(() => ({ action: 'deny' }));

  // renderer/GPU 崩溃：重载页面（backend 由独立看门狗负责）
  mainWindow.webContents.on('render-process-gone', (_e, details) => {
    console.error('[renderer] gone:', details.reason);
    if (details.reason !== 'clean-exit' && !app.isQuitting) {
      mainWindow.webContents.reload();
    }
  });

  // 关闭 -> 隐藏到托盘（后端继续运行）
  mainWindow.on('close', (e) => {
    if (!app.isQuitting) {
      e.preventDefault();
      mainWindow.hide();
    }
  });
}

function createTray(port) {
  const iconPath = path.join(__dirname, 'build', 'icon.png');
  const image = fs.existsSync(iconPath)
    ? nativeImage.createFromPath(iconPath).resize({ width: 16, height: 16 })
    : nativeImage.createEmpty();
  tray = new Tray(image);
  tray.setToolTip('Media Squirrel');
  tray.setContextMenu(Menu.buildFromTemplate([
    { label: '显示窗口', click: () => (mainWindow ? (mainWindow.show(), mainWindow.focus()) : null) },
    { label: '隐藏窗口', click: () => mainWindow && mainWindow.hide() },
    { type: 'separator' },
    { label: '检查更新', click: () => { mainWindow.show(); void updates?.check(); } },
    { label: '安装已下载更新', click: () => { mainWindow.show(); void updates?.install(); } },
    { type: 'separator' },
    { label: '退出', click: () => app.quit() },
  ]));
  tray.on('double-click', () => mainWindow && mainWindow.show());
}

// ---------- GitHub Releases updates (unsigned Windows distribution) ----------
const { autoUpdater } = require('electron-updater');
const { createUpdates } = require('./updates.cjs');
let updates = null;

function updateBackend(action) {
  return new Promise((resolve, reject) => {
    const req = http.request({ host: '127.0.0.1', port: currentPort,
      path: '/api/desktop/update-lock', method: 'POST', timeout: 3000,
      headers: { 'Content-Type': 'application/json', 'X-Desktop-Token': desktopToken } }, res => {
      let body = '';
      res.on('data', chunk => { body += chunk; if (body.length > 4096) req.destroy(new Error('Invalid update status')); });
      res.on('error', reject);
      res.on('end', () => {
        try {
          if (res.statusCode !== 200) throw new Error('Cannot confirm task status');
          const result = JSON.parse(body);
          if (!Number.isInteger(result.active) || result.active < 0 || typeof result.locked !== 'boolean') throw new Error('Invalid task status');
          resolve(result);
        } catch (error) { reject(error); }
      });
    });
    req.on('error', reject);
    req.on('timeout', () => req.destroy(new Error('Task status request timed out')));
    req.end(JSON.stringify({ action }));
  });
}

function setupAutoUpdater() {
  if (IS_DEV || !app.isPackaged) return;
  updates = createUpdates({ updater: autoUpdater, app, dialog, backend: updateBackend,
    stopBackend: killBackend,
    resumeBackend: async () => {
      if (!backendProc) await startBackend(currentPort);
      await waitForBackend(currentPort);
    },
  });
  void updates.check();
}

for (const action of ['state', 'check', 'install']) {
  ipcMain.handle('updates:' + action, (event) => {
    const frame = event.senderFrame;
    if (!mainWindow || event.sender !== mainWindow.webContents || !frame ||
        frame !== mainWindow.webContents.mainFrame ||
        new URL(frame.url).origin !== new URL(IS_DEV ? DEV_URL : `http://127.0.0.1:${currentPort}`).origin) {
      throw new Error('Untrusted update request');
    }
    if (!updates) return { status: 'disabled', version: app.getVersion(), message: '开发模式不检查更新', canInstall: false };
    return action === 'state' ? updates.getState() : updates[action]();
  });
}

// ---------- 单实例 ----------
const gotLock = app.requestSingleInstanceLock();
if (!gotLock) {
  app.quit();
} else {
  app.on('second-instance', () => {
    if (mainWindow) {
      mainWindow.show();
      mainWindow.focus();
    }
  });

  app.whenReady().then(async () => {
    try {
      // DEV 模式: 后端由 concurrently 单独启动，Electron 只做健康检查；
      // 生产模式: Electron 负责启动后端（自动选可用端口）
      const port = IS_DEV ? Number(process.env.MS_PORT || 8642) : await pickFreePort();
      if (!IS_DEV) {
        await startBackend(port);
      }
      await waitForBackend(port);
      const url = IS_DEV ? DEV_URL : `http://127.0.0.1:${port}`;
      createWindow(url);
      createTray(port);
      // 主窗口就绪后异步检查更新（不阻塞启动）
      setupAutoUpdater();
    } catch (err) {
      dialog.showErrorBox('Media Squirrel 启动失败', String(err && err.message || err));
      app.quit();
    }
  });

  app.on('window-all-closed', (e) => { /* 托盘常驻，不退出 */ });

  app.on('before-quit', event => {
    app.isQuitting = true;
    if (backendProc || stopping) {
      event.preventDefault();
      killBackend().then(() => app.quit()).catch(error => {
        app.isQuitting = false;
        dialog.showErrorBox('退出失败', error.message);
      });
    }
  });
}
