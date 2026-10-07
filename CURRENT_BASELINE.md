# CURRENT_BASELINE — 当前代码基线报告

> 生成时间：2026-10-07 · 以本地 Git 工作区为唯一事实来源

| # | 项目 | 值 |
|---|---|---|
| 1 | Git commit | `4d9f478` (main, 工作区干净) |
| 2 | 应用版本 | 1.0.0（package.json）；backend 无独立版本号 |
| 3 | 桌面框架 | Electron 33.4.11（electron-builder 25.1.8, NSIS） |
| 4 | 前端框架 | Vue 3.5.43 + Vite 6.4.3 + vue-router 4.6.4 |
| 5 | Node 版本要求 | ≥18（当前开发机 v24.13.0，未在 package.json 声明 engines） |
| 6 | Python 版本要求 | 3.10+（当前开发机 3.14.2，requirements.txt 未锁版本） |
| 7 | Electron | 33.4.11 |
| 8 | electron-builder | 25.1.8 |
| 9 | FastAPI | 0.136.0 |
| 10 | Uvicorn | 0.44.0 |
| 11 | Playwright | 1.62.0（需要 chromium/headless-shell revision **1234**、ffmpeg 1011） |
| 12 | PyInstaller | 6.20.0（onedir，build_backend.ps1 驱动） |
| 13 | 构建命令 | `npm run desktop:build`（icon→frontend→backend→electron-builder） |
| 14 | 安装包输出 | `release/Media-Squirrel-Setup-1.0.0.exe` |
| 15 | 安装包大小 | **646 MB** |
| 16 | backend 大小 | 1,831 MB（onedir 未压缩） |
| 17 | Playwright 浏览器 | **1,386 MB**（含 1228+1234 双 revision，实际只需 1234 → 应为 ~701 MB） |
| 18 | 用户数据目录 | `%LOCALAPPDATA%\Media Squirrel\`（frozen；`MS_DATA_DIR` 可覆盖） |
| 19 | 自动更新 | ❌ 无 |
| 20 | 代码签名 | ❌ 无（builder 日志 "signing is skipped"） |

## 现状清单（第一阶段侦察结论）

**已存在**：Electron 壳（main.cjs/preload.cjs，contextIsolation✓ sandbox✓ 单实例✓ 托盘✓ 树杀后端✓）、PyInstaller 脚本（build_backend.ps1）、playwright 随包分发（PLAYWRIGHT_BROWSERS_PATH）、NSIS 安装器（自定义目录/快捷方式/卸载）、127.0.0.1 随机端口 + 健康检查、用户数据 LOCALAPPDATA 分离。

**不存在**：`.github/workflows`（无 CI）、electron-updater（无自动更新）、更新清单签名（Ed25519）、Authenticode 签名、版本锁定（requirements.txt 全是 `>=`）、preview 模式命令。

**遗留垃圾**：`src-tauri` 已删但 `backend-dist`/`release`/`build` 为旧构建产物；`media-squirrel-backend.spec`/`MediaSquirrelBackend.spec` 为 PyInstaller 残留 spec（构建脚本每次重建，属生成物）；`build_portable.ps1` 为 Tauri 时代遗留。

## 依赖实测（npm ls / pip list）

根：concurrently 9.2.4、cross-env 7.0.3、electron 33.4.11、electron-builder 25.1.8、wait-on 8.0.5
前端：@vitejs/plugin-vue 5.2.4、vite 6.4.3、vue 3.5.43、vue-router 4.6.4
Python：fastapi 0.136.0、uvicorn 0.44.0、playwright 1.62.0、PyInstaller 6.20.0、browserforge 1.2.4、opencv-python 5.0.0.93、patchright 1.62.2（应急备用）、pystray 0.19.5、scrapling 0.4.15（**已弃用但仍装在开发机**，打包已排除）
