// Media Squirrel — Electron 主进程
// 职责: 启动/关闭 Python 后端、健康检查、主窗口、系统托盘。
// 绝不使用外部浏览器（无 webbrowser.open / shell.openExternal 打开主界面）。
'use strict';

const { app, BrowserWindow, Tray, Menu, nativeImage, dialog } = require('electron');
const { spawn } = require('child_process');
const http = require('http');
const net = require('net');
const path = require('path');
const fs = require('fs');

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
  return path.join(local, 'Media Squirrel');
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

async function startBackend(port) {
  const exe = backendExePath();
  const dataDir = userDataDir();
  fs.mkdirSync(dataDir, { recursive: true });

  // Playwright 浏览器目录：优先随包分发的 chromium，其次用户已装的 ms-playwright
  const env = { ...process.env, MS_PORT: String(port) };
  const bundledBrowsers = path.join(process.resourcesPath || '', 'playwright-browsers');
  if (!IS_DEV && fs.existsSync(bundledBrowsers)) {
    env.PLAYWRIGHT_BROWSERS_PATH = bundledBrowsers;
  }

  if (exe) {
    backendProc = spawn(exe, ['--no-browser', '--no-tray', '--port', String(port)], {
      cwd: dataDir,
      env,
      windowsHide: true,
    });
  } else if (IS_DEV) {
    // 开发模式：python 源码
    const root = path.join(__dirname, '..');
    backendProc = spawn('python', ['run.py', '--no-browser', '--no-tray', '--port', String(port)], {
      cwd: root, env, windowsHide: true,
    });
  } else {
    throw new Error('未找到 MediaSquirrelBackend.exe');
  }

  backendProc.stdout.on('data', (d) => process.stdout.write(`[backend] ${d}`));
  backendProc.stderr.on('data', (d) => process.stderr.write(`[backend] ${d}`));
  backendProc.on('exit', (code) => {
    console.log(`[backend] 退出 code=${code}`);
    backendProc = null;
  });
}

function killBackend() {
  if (!backendProc || backendProc.killed) return;
  const pid = backendProc.pid;
  try {
    // Windows: 树杀（后端会 spawn 下载脚本/浏览器子进程）
    spawn('taskkill', ['/PID', String(pid), '/T', '/F'], { windowsHide: true });
  } catch (e) {
    try { backendProc.kill('SIGKILL'); } catch (_) { /* noop */ }
  }
  backendProc = null;
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
    { label: '退出', click: () => app.quit() },
  ]));
  tray.on('double-click', () => mainWindow && mainWindow.show());
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
      const port = IS_DEV ? 8642 : await pickFreePort();
      if (!IS_DEV) {
        await startBackend(port);
      }
      await waitForBackend(port);
      const url = IS_DEV ? DEV_URL : `http://127.0.0.1:${port}`;
      createWindow(url);
      createTray(port);
    } catch (err) {
      dialog.showErrorBox('Media Squirrel 启动失败', String(err && err.message || err));
      app.quit();
    }
  });

  app.on('window-all-closed', (e) => { /* 托盘常驻，不退出 */ });

  app.on('before-quit', () => {
    app.isQuitting = true;
    killBackend();
  });

  process.on('exit', killBackend);
}
