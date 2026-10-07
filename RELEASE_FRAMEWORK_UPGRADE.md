# Electron 44 升级记录

起始 HEAD：`541debcda326421a3ae274f5edafdc437a786d36`，起始工作区干净。

## 版本选择与兼容审查

- Electron 从 lockfile 的 `33.4.11` 升级并精确锁定为 `44.6.0`。
- [官方 44.6.0 release](https://github.com/electron/electron/releases/tag/v44.6.0) 的 `prerelease=false`，发布时间为北京时间 2026-10-07 06:31:27。npm 官方 registry 同样提供该版本，要求 Node >=22.12.0。
- 阅读了[官方 breaking changes](https://github.com/electron/electron/blob/main/docs/breaking-changes.md)。本项目 Windows x64 不依赖被删除的 ia32、renderer clipboard、旧 extension 或 bitmap API。现有 preload 仅使用 contextBridge 和版本信息；BrowserWindow 的隔离配置保留。窗口生命周期、Tray、singleInstanceLock、child_process 和 before-quit 均需要真实运行检查，不能仅凭接口仍存在判定通过。
- electron-builder 保留 `26.15.3`，electron-updater 保留 `6.8.9`；官方 `updateManifest` 仍在尚未稳定发布的 v27 文档，升级到 v26 分支最新补丁也不能据此宣称支持该功能。
- Node 最低版本同步为 `22.12.0`；lockfile 根版本从旧的 `1.0.0` 纠正为 package.json 的 `1.1.0`。
- concurrently 锁死 shell-quote 1.9.0，普通 `npm update shell-quote` 无法修复。因此仅对此依赖使用 1.11.0 override；npm audit 的 2 项 critical 消失，仍有 8 项构建链 moderate，未使用强制批量升级。

## 实测驱动的最小修复

1. 既有本地服务占用 8642 时，开发启动失败。Electron 和 Vite 现共同读取 `MS_PORT`；Vite 固定 5173，避免悄悄换端口后 Electron 打开错误服务。
2. 自定义 `MS_DATA_DIR` 曾同时改变源码资源路径，导致脚本清单与前端资源丢失。现在数据目录与只读源码资源目录分离；冻结版仍使用 `_MEIPASS`。
3. 后端 stdin 看门狗占用 Electron 管道时，下载子进程继承该 stdin 会阻塞。真实冻结进程最小复现：继承 stdin 15 秒超时，DEVNULL 立即输出 `CHILD_STARTED`。下载子进程现在显式使用 DEVNULL；父后端看门狗保持原样。
4. 实际微博下载出现“封面成功”日志但中文路径无 JPG。改用 `cv2.imencode()` 加 Python 文件写入，并验证真实 JPEG 可解码；不改下载流程或移除 OpenCV。
5. backend bundle 加入 package.json，使 `/api/health` 能报告应用真实版本，而非 `dev`。

## 可重复验证

依次执行过 `npm ci`、`npm run desktop:dev`、`npm run desktop:preview`、`npm run desktop:build`。修复后完整重建，安装包输出在独立的 `release/verification-electron44/`，未覆盖原 `release/Media-Squirrel-Setup-1.1.0.exe`。

```powershell
# 新构建安装包只用于本地验收，不发布
npm run desktop:build -- --publish never --config.directories.output=release/verification-electron44

# 真实 Electron / backend 集成检查（使用独立数据目录）
.\node_modules\.bin\electron.cmd electron/tests/framework-smoke.cjs

# 真实视频编码、中文路径 JPEG 写入、失败返回值
python -m unittest discover -s electron/tests -p 'test*.py'
```

`framework-smoke.cjs` 不 mock Electron 或 HTTP，检查实际窗口、preload、Node 隔离、API、WebSocket、动态端口、关闭隐藏、第二实例恢复及 SQLite。它不代替安装升级或原生托盘菜单点击验收。测试脚本已从正式 ASAR 文件列表排除。

最终实测结果、32 项状态、签名和体积以 `RELEASE_FINAL_REPORT.md` 为准。完整发布基础设施尚未完成，应用版本暂保留 1.1.0，不能提前标记 v1.2.0 正式版。
