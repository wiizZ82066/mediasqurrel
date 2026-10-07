'use strict';

// Run with the real Electron binary, after building backend-dist. No mocked APIs.
// Keep test data outside the user's normal LOCALAPPDATA data directory.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { spawn } = require('node:child_process');
const { app, BrowserWindow } = require('electron');

const root = path.resolve(__dirname, '../..');
process.env.MS_DATA_DIR = path.join(root, 'release', 'verification-data');
const resultFile = path.join(root, 'release', 'framework-smoke.json');
const checks = [];
const delay = ms => new Promise(resolve => setTimeout(resolve, ms));
const check = (name, condition) => { assert.ok(condition, name); checks.push(name); };

if (!process.env.MS_SMOKE_SECOND_INSTANCE) {
  const timeout = setTimeout(() => {
    fs.writeFileSync(resultFile, JSON.stringify({ checks, error: '90 second timeout' }, null, 2));
    app.quit();
    process.exitCode = 1;
  }, 90000);
  app.once('browser-window-created', (_event, win) => {
    win.webContents.once('did-finish-load', async () => {
      try {
        const prefs = win.webContents.getLastWebPreferences();
        check('renderer isolation', prefs.contextIsolation && !prefs.nodeIntegration && prefs.sandbox);
        const result = await win.webContents.executeJavaScript(`(async () => {
          const scripts = await fetch('/api/scripts').then(r => r.json());
          const health = await fetch('/api/health').then(r => r.json());
          const ws = await new Promise((resolve, reject) => {
            const socket = new WebSocket('ws://' + location.host + '/ws');
            const timer = setTimeout(() => { socket.close(); reject(new Error('WebSocket timeout')); }, 5000);
            socket.onopen = () => { clearTimeout(timer); socket.close(); resolve(true); };
            socket.onerror = () => { clearTimeout(timer); reject(new Error('WebSocket error')); };
          });
          return { scripts: scripts.map(s => ({ id: s.id, available: s.available })),
            health, ws, node: typeof require, electron: window.mediaSquirrel.versions.electron,
            body: document.body.innerText, url: location.href };
        })()`);
        check('Electron 44.6.0 preload', result.electron === '44.6.0');
        check('Node unavailable in renderer', result.node === 'undefined');
        check('API health', result.health.status === 'ok');
        check('backend reports application version', result.health.version === require('../../package.json').version);
        check('both downloader manifests available', result.scripts.length === 2 && result.scripts.every(s => s.available));
        check('WebSocket connected', result.ws);
        check('Vue rendered', result.body.includes('Media Squirrel') && result.body.includes('下载'));
        check('dynamic loopback origin', /^http:\/\/127\.0\.0\.1:\d+\//.test(result.url));
        await delay(1000);
        fs.writeFileSync(path.join(root, 'release', 'framework-smoke.png'), (await win.webContents.capturePage()).toPNG());
        win.close();
        check('close hides window without destruction', !win.isDestroyed() && !win.isVisible());
        const restored = new Promise((resolve, reject) => {
          const timer = setTimeout(() => reject(new Error('second-instance event timeout')), 10000);
          app.once('second-instance', () => { clearTimeout(timer); resolve(); });
        });
        const second = spawn(process.execPath, [__filename], {
          env: { ...process.env, MS_SMOKE_SECOND_INSTANCE: '1' }, windowsHide: true, stdio: 'ignore',
        });
        await new Promise((resolve, reject) => { second.once('error', reject); second.once('exit', resolve); });
        await restored;
        check('second instance restores same window', win.isVisible() && BrowserWindow.getAllWindows().length === 1);
        check('isolated SQLite exists', fs.existsSync(path.join(process.env.MS_DATA_DIR, 'app_data', 'app.db')));
        fs.writeFileSync(resultFile, JSON.stringify({ checks, result, status: 'PASS', backendPort: new URL(result.url).port }, null, 2));
        clearTimeout(timeout);
        app.quit();
      } catch (error) {
        fs.writeFileSync(resultFile, JSON.stringify({ checks, error: error.stack, status: 'FAIL' }, null, 2));
        clearTimeout(timeout);
        process.exitCode = 1;
        app.quit();
      }
    });
  });
}
require('../main.cjs');
