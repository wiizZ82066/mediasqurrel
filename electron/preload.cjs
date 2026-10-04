// 预加载脚本：安全隔离桥。
// 主界面通过 HTTP/WebSocket 与本地 FastAPI 通信，无需 Node 能力，
// 此处仅暴露最小版本信息（contextIsolation: true / nodeIntegration: false）。
'use strict';
const { contextBridge } = require('electron');

contextBridge.exposeInMainWorld('mediaSquirrel', {
  desktop: true,
  versions: {
    electron: process.versions.electron,
    chrome: process.versions.chrome,
  },
});
