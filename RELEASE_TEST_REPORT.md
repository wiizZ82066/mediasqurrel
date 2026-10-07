# RELEASE_TEST_REPORT — 发布验收测试

> 基于优化后构建 `Media-Squirrel-Setup-1.1.0.exe`（424 MB）

| # | 测试项 | 结果 | 备注 |
|---|---|---|---|
| 1 | 安装（NSIS） | PASS | 自定义目录/快捷方式/卸载 |
| 2 | 启动 | PASS | 双击 exe → 独立窗口 |
| 3 | 独立窗口 | PASS | Electron BrowserWindow |
| 4 | 不打开外部浏览器 | PASS | 无 Chrome/Edge 窗口 |
| 5 | 单实例 | PASS | 二次启动唤醒已有窗口 |
| 6 | 托盘显示 | PASS | 松鼠图标 + 菜单 |
| 7 | 隐藏窗口（关闭按钮） | PASS | 后端继续运行 |
| 8 | 恢复窗口（托盘双击） | PASS | |
| 9 | 真正退出（托盘→退出） | PASS | |
| 10 | backend 关闭 | PASS | before-quit 树杀 |
| 11 | 无残留 Python | PASS | taskkill /T /F |
| 12 | 无残留 downloader | PASS | 子进程树终止 |
| 13 | 无残留 Chromium | PASS | |
| 14 | API | PASS | /api/scripts 200 |
| 15 | WebSocket | PASS | /ws 连接正常 |
| 16 | SQLite | PASS | LOCALAPPDATA 用户数据 |
| 17 | 用户数据目录 | PASS | %LOCALAPPDATA%\Media Squirrel\ |
| 18 | 媒体库 | PASS | 扫描+缩略图+时间线 |
| 19 | 微博下载 | PASS | 原图+Live 图 |
| 20 | 抖音下载 | PASS | 无水印视频+官方封面 |
| 21 | Cookie | PASS | 登录态本地持久化 |
| 22 | Playwright | PASS | chromium-1234 + headless_shell |
| 23 | 任务取消 | PASS | 显示 cancelled（非 failed） |
| 24 | 取消后无残留进程 | PASS | taskkill /T /F |
| 25 | 更新检查 | NOT TESTED | 需 GitHub Release 发布 |
| 26 | 更新下载 | NOT TESTED | 同上 |
| 27 | 更新签名验证 | NOT TESTED | 同上（Ed25519 v27 待发布） |
| 28 | 更新安装 | NOT TESTED | 同上 |
| 29 | Authenticode 签名 | NOT TESTED | 需证书（CI 配置就绪） |
| 30 | 安装器卸载 | PASS | 控制面板卸载正常 |
| 31 | 再次安装 | PASS | |
| 32 | 升级后用户数据保留 | PASS | LOCALAPPDATA 独立于安装目录 |

## NOT TESTED 项说明

- **25-28 自动更新**：需要先在 GitHub 上发布 v1.1.0 Release（含 Setup.exe + latest.yml），再安装旧版本测试更新流程。CI workflow 已就绪。
- **29 Authenticode**：需要 OV/Azure Trusted Signing 证书。CI 已配置强制校验（无证书 → FAIL）。当前 424 MB 构建未签名。
- **Ed25519 manifest signing**：electron-builder v26.x 不支持（v27 特性）。密钥对已生成，v27 发布后添加 `updateManifest` 配置即可启用。
