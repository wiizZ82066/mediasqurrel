# RELEASE_AUDIT — 发布前代码审计

> 基于 `4d9f478`。P0=发布阻塞 / P1=强烈建议 / P2=后续优化

## P0（发布阻塞）

| # | 模块 | 问题 | 位置 |
|---|---|---|---|
| P0-1 | C.下载任务 | **取消的任务显示为 failed**：`cancel()` kill 进程后 `_run` 里 `code≠0 → status="failed"`，L194 finally 兜底同样把被杀进程标 failed。取消语义丢失 | task_manager.py L183/L194/L217-222 |
| P0-2 | C.下载任务 | **取消不杀进程树**：`proc.kill()` 在 Windows 只杀直接子进程（python exe），其派生的 Chromium/Chrome 残留后台；审计点明确要求进程树终止 | task_manager.py L219 |
| P0-3 | A.Electron | **backend 提前退出无感知**：`backendProc.on('exit')` 仅打日志。后端崩溃后 UI 全挂（API 全 404/WS 断开）但窗口仍在，用户面对死界面无任何提示与恢复 | electron/main.cjs |
| P0-4 | 构建 | **Playwright 双 revision 打包**：1228+1234 两套 chromium+headless_shell 共 1386MB，playwright 1.62.0 仅需 1234（~701MB），安装包虚胖 ~150MB+（压缩后） | build_backend.ps1 |

## P1（强烈建议）

| # | 模块 | 问题 |
|---|---|---|
| P1-1 | A.Electron | renderer/GPU 崩溃无处理（未监听 render-process-gone） |
| P1-2 | A.Electron | Electron 主进程被强杀时 backend 残留（before-quit 不触发）→ backend 加 stdin 看门狗（父进程退出→stdin EOF→自杀） |
| P1-3 | 依赖 | requirements.txt 全 `>=` 不可重复构建；package.json 无 engines |
| P1-4 | 发布 | 无自动更新（electron-updater）；无更新清单签名；无代码签名 |
| P1-5 | CI | 无 GitHub Actions；构建依赖开发机 %LOCALAPPDATA%\ms-playwright（禁止） |
| P1-6 | 体积 | patchright 打进 backend（应急开关默认关闭，动态 import 已 try/except 保护，可排除省一份 node driver ~80MB） |

## P2（后续优化）

| # | 问题 |
|---|---|
| P2-1 | 生产 CORS 仍允许 5173（无实际危害：仅监听 127.0.0.1；条件化更严谨） |
| P2-2 | FastAPI on_event 弃用告警（lifespan 迁移）；无 CSP 响应头（内容全本地） |
| P2-3 | 构建垃圾：*.spec 残留、build_portable.ps1（Tauri 时代） |
| P2-4 | scrapling 仍装于开发机（打包已排除，仅环境噪音） |

## 通过项（无需修改）

- ✅ 独立窗口，无 webbrowser.open / shell.openExternal 打开主界面
- ✅ contextIsolation:true / nodeIntegration:false / sandbox:true / preload 最小化
- ✅ 单实例 + 第二实例唤醒 + 关闭隐藏托盘 + 托盘菜单（显示/隐藏/退出）
- ✅ 随机端口 + 127.0.0.1 only + /api/scripts 健康检查 60s 超时 + 启动失败弹窗
- ✅ 用户数据 LOCALAPPDATA 分离（DB/thumbs/cover_cache/douyin_profile/library 全走 config.BASE_DIR，实测路径核对）；StaticFiles 崩溃已修
- ✅ frozen 路径（RESOURCE_DIR/_MEIPASS、--internal-run 子进程、douyin_downloader 输出目录）
- ✅ 敏感信息：cookies/DB 在 app_data/（gitignore）；无证书/密钥入库；Ed25519 私钥将仅入 CI secret
- ✅ NSIS：自定义目录/桌面+开始菜单/卸载/升级（GUID 稳定）
