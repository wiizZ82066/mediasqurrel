// 预加载脚本：安全隔离桥。
// 主界面通过 HTTP/WebSocket 与本地 FastAPI 通信，无需 Node 能力，
// 仅暴露版本信息及固定的更新操作，不暴露通用 IPC、文件或 Node API。
'use strict';
const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('mediaSquirrel', {
  desktop: true,
  updates: {
    state: () => ipcRenderer.invoke('updates:state'),
    check: () => ipcRenderer.invoke('updates:check'),
    install: () => ipcRenderer.invoke('updates:install'),
  },
  versions: {
    electron: process.versions.electron,
    chrome: process.versions.chrome,
  },
});
