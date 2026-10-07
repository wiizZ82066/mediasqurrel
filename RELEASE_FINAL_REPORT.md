# Media Squirrel Windows RC 验收报告

日期：2026-10-07 至 2026-10-08（北京时间）。状态：**RELEASE CANDIDATE**。

用户已确认 Windows 签名凭据尚未准备，先保留 RC。没有创建正式 Release、tag 或推送 main，没有提前升级为 v1.2.0。下述本地未签名产物仅供验收。

## A–J 代码与运行环境

| 项目 | 实际值 |
|---|---|
| A 当前 Git commit | 代码提交 `4c65eb4`（本地 main）；起始 HEAD `541debcda326421a3ae274f5edafdc437a786d36`。产物在提交前从同一工作区构建；报告单独提交，不宣称已推送 |
| B 应用版本 | 1.1.0；完整发布基础设施完成后才使用 1.2.0 |
| C Electron | 44.6.0，稳定版，精确锁定 |
| D electron-builder / updater | 26.15.3 / 6.8.9 |
| E Vue / Vite | 3.5.43 / 6.4.3 |
| F Python | 本机 3.14.2；现有 CI 配置 3.12，不能视为相同构建环境 |
| G FastAPI | 0.136.0 |
| H Uvicorn | 0.44.0 |
| I Playwright | 1.62.0；随包 Chromium revision 1234 |
| J PyInstaller | 6.20.0 |

Electron 官方版本和 breaking changes、依赖选择与最小修复见 [升级记录](RELEASE_FRAMEWORK_UPGRADE.md)。没有重构 Vue、FastAPI 或下载器，没有移除 Chromium、headless_shell、OpenCV、browserforge 或 fingerprint 资源。

## K–M 最终体积

本次最终构建：`release/verification-electron44/Media-Squirrel-Setup-1.1.0.exe`。原根目录 1.1.0 安装包保留，没有覆盖。

| 项目 | 字节 | MiB |
|---|---:|---:|
| K 安装包 | 449795484 | 428.96 |
| L backend-dist（含浏览器） | 1095507564 | 1044.76 |
| backend 核心 | 360678130 | 343.97 |
| 随包浏览器 | 734829434 | 700.79 |

“424 MB 为当前功能集合下的合理成本，暂不继续压缩。”这是旧基线的结论；Electron 44 本次实际安装包约 429 MiB，不能把旧数字冒充新构建结果。

最大文件 Top 20（backend-dist）：

| # | 路径 | 字节 |
|---|---|---:|
| 1 | `backend-dist\playwright-browsers\chromium-1234\chrome-win64\chrome.dll` | 297987584 |
| 2 | `backend-dist\playwright-browsers\chromium_headless_shell-1234\chrome-headless-shell-win64\chrome-headless-shell.exe` | 211223552 |
| 3 | `backend-dist\MediaSquirrelBackend\_internal\playwright\driver\node.exe` | 92540232 |
| 4 | `backend-dist\MediaSquirrelBackend\_internal\cv2\cv2.pyd` | 86293504 |
| 5 | `backend-dist\MediaSquirrelBackend\_internal\cv2\opencv_videoio_ffmpeg500_64.dll` | 30876160 |
| 6 | `backend-dist\playwright-browsers\chromium-1234\chrome-win64\dxcompiler.dll` | 25752064 |
| 7 | `backend-dist\MediaSquirrelBackend\MediaSquirrelBackend.exe` | 22742911 |
| 8 | `backend-dist\playwright-browsers\chromium-1234\chrome-win64\resources.pak` | 21265611 |
| 9 | `backend-dist\MediaSquirrelBackend\_internal\numpy.libs\libscipy_openblas64_-63c857e738469261263c764a36be9436.dll` | 20415488 |
| 10 | `backend-dist\playwright-browsers\chromium-1234\chrome-win64\icudtl.dat` | 10876560 |
| 11 | `backend-dist\playwright-browsers\chromium_headless_shell-1234\chrome-headless-shell-win64\icudtl.dat` | 10876560 |
| 12 | `backend-dist\MediaSquirrelBackend\_internal\cryptography\hazmat\bindings\_rust.pyd` | 9744384 |
| 13 | `backend-dist\MediaSquirrelBackend\_internal\PIL\_avif.cp314-win_amd64.pyd` | 7892992 |
| 14 | `backend-dist\MediaSquirrelBackend\_internal\python314.dll` | 6760792 |
| 15 | `backend-dist\playwright-browsers\chromium-1234\chrome-win64\setup.exe` | 5797376 |
| 16 | `backend-dist\playwright-browsers\chromium-1234\chrome-win64\vk_swiftshader.dll` | 5447168 |
| 17 | `backend-dist\playwright-browsers\chromium_headless_shell-1234\chrome-headless-shell-win64\vk_swiftshader.dll` | 5447168 |
| 18 | `backend-dist\MediaSquirrelBackend\_internal\pydantic_core\_pydantic_core.cp314-win_amd64.pyd` | 5427712 |
| 19 | `backend-dist\MediaSquirrelBackend\_internal\libcrypto-3.dll` | 5229424 |
| 20 | `backend-dist\playwright-browsers\chromium-1234\chrome-win64\D3DCompiler_47.dll` | 4741488 |

## N 自动更新

真实 GitHub Releases 列表为空，没有两个已签名测试版本。发现新版、下载、安装、重启、任务保护及失败场景闭环均未完成。实际安装版遇到 `No published versions on GitHub` 后仍完成两个真实下载，该限定场景通过，不能外推到断网或损坏包。

详见 [更新验收记录](RELEASE_UPDATE_TEST.md)。当前 `autoInstallOnAppQuit=true` 会绕过手动安装的运行任务检查；任务查询失败也被视为无任务。依照“框架核心回归全部通过后再推进更新”的要求，本轮没有把这些后续阶段伪装成完成。

## O Ed25519 manifest signing

**FAIL（当前实现审计）**。最终 `latest.yml` 没有 signature，`app-update.yml` 没有可信 Ed25519 公钥。当前库解析器接受无签名及 `signature: invalid`；这只是解析器实测，不是四种客户端端到端安全测试。合法 key、错误 key、篡改清单拒绝的完整客户端闭环仍 NOT TESTED。

已核对 npm 官方包：builder latest 26.15.3，v26 标签 26.17.0；两者没有官方 updateManifest 配置。v27 为 alpha，未引入预发布依赖，也未自制替代官方签名方案。

## P Authenticode、时间戳与 publisher

**FAIL（本地验收产物未签名）**。`Get-AuthenticodeSignature` 实际检查安装器、主程序、后端，全部 `NotSigned`。

| 产物 | 状态 | subject / issuer / valid from / valid to / thumbprint / timestamp |
|---|---|---|
| Media-Squirrel-Setup-1.1.0.exe | NotSigned | 全部 N/A，没有证书 |
| Media Squirrel.exe | NotSigned | 全部 N/A，没有证书 |
| MediaSquirrelBackend.exe | NotSigned | 全部 N/A，没有证书 |

RFC3161 验证及 publisher 一致性 NOT TESTED。当前用户证书库未找到代码签名证书，仓库 Actions Secrets 名称列表为空；用户确认尚未准备签名方案。私钥、Cookie 和下载产物未加入 Git。

## Q 用户数据保留

本轮真实下载使用 `release/final-verification-data/` 隔离数据目录。卸载和重装后比对该目录 11 个文件的 SHA256，全部保持一致，包括数据库、Cookie 和下载文件。但这不代表 `%LOCALAPPDATA%` 的真实版本升级迁移通过。没有真实签名版本升级，因此 SQLite、Cookies、媒体库配置、下载目录和不产生第二套正式数据的升级闭环均 **NOT TESTED**。

## R 32 项回归

旧报告实际为 27 PASS、5 NOT TESTED，共 32 项。本表仅记录本次证据，不继承旧 PASS。

17 PASS、2 FAIL、13 NOT TESTED，共 32 项。

| # | 测试项 | 结果 | 证据 / 限制 |
|---|---|---|---|
| 1 | 安装（NSIS） | PASS | 独立测试目录实际安装、启动 |
| 2 | 启动 | PASS | 安装版实际启动；长期运行限制见下文 |
| 3 | 独立窗口 | PASS | 真实 Electron BrowserWindow 和安装版页面 |
| 4 | 不打开外部浏览器 | PASS | 主界面在 Electron；诊断工具浏览器另计 |
| 5 | 单实例 | PASS | 真实第二实例恢复同一窗口 |
| 6 | 托盘显示 | NOT TESTED | 未完成原生图标与菜单操作 |
| 7 | 隐藏窗口 | PASS | close 后隐藏且未销毁 |
| 8 | 托盘双击恢复 | NOT TESTED | 不能以第二实例恢复代替 |
| 9 | 托盘真正退出 | NOT TESTED | 未完成菜单点击验收 |
| 10 | backend 关闭 | PASS | framework-smoke app.quit 后后端 PID 消失，限定此路径 |
| 11 | 无残留 Python | NOT TESTED | 未完成原生退出全链路 |
| 12 | 无残留 downloader | NOT TESTED | 未完成各运行状态的退出全链路 |
| 13 | 无残留 Chromium | NOT TESTED | 未完成原生退出全链路 |
| 14 | API | PASS | 实际 health/scripts/tasks；长期运行仍须复测 |
| 15 | WebSocket | PASS | 真实 /ws 连接 |
| 16 | SQLite | PASS | 隔离目录实际创建数据库 |
| 17 | 默认用户数据目录 | NOT TESTED | 本轮使用 MS_DATA_DIR，未覆盖正式 LOCALAPPDATA 数据 |
| 18 | 媒体库 | NOT TESTED | 测试输出层级不符合默认扫描布局，未验收时间线和缩略图 |
| 19 | 微博下载 | PASS | 安装版真实链接，2 MOV + 2 JPG + context.md |
| 20 | 抖音下载 | PASS | 安装版真实链接，MP4 + 官方封面 + context.md |
| 21 | Cookie | PASS | 隔离副本登录态用于实际抖音下载，未输出凭据 |
| 22 | Playwright | PASS | 冻结后端启动随包浏览器并操作页面 |
| 23 | 任务取消 | PASS | 最终安装版 running → cancelled |
| 24 | 取消后无残留进程 | NOT TESTED | 未记录取消时完整后代 PID 集合 |
| 25 | 更新检查 | NOT TESTED | 仅验证无已发布版本的错误处理，未发现真实新版 |
| 26 | 更新下载 | NOT TESTED | 没有签名测试 Releases |
| 27 | 更新签名验证 | FAIL | 当前产物缺签名/可信公钥，解析器接受无效 signature |
| 28 | 更新安装 | NOT TESTED | 没有真实升级目标 |
| 29 | Authenticode | FAIL | 三个 EXE 实测 NotSigned |
| 30 | 安装器卸载 | PASS | 清理诊断句柄后实际卸载退出 0，主 EXE 删除，隔离数据哈希不变；初次残留见下文 |
| 31 | 再次安装 | PASS | 重装退出码 0、app.asar 恢复；实际启动，health 版本 1.1.0，任务取消通过 |
| 32 | 升级后用户数据保留 | NOT TESTED | 没有真实两版本升级，隔离存放不等于迁移通过 |

验证过程及限制：

- 依次执行 npm ci、desktop:dev、desktop:preview、desktop:build；最后一次完整构建成功。开发用 MS_PORT=18642 绕过既有服务占用，测试不结束用户其他服务。
- 真实 Electron 集成脚本 12 项断言通过；包括隔离、preload、Vue、API、WebSocket、动态端口、关闭隐藏、第二实例恢复、SQLite 和真实后端版本。
- 最终安装版微博下载成功，实际 5 个文件：2 MOV、2 JPG、context.md。抖音成功，实际 3 个文件：MP4、封面、context.md。链接由用户提供。
- 中文路径封面回归使用真实视频编码、JPEG 解码及失败路径，1 项 unittest 通过。随包浏览器实际启动及页面操作通过。
- 最终安装版任务从 running 取消到 cancelled；不是 failed。没有据此宣称所有取消时机的进程清理均已验证。
- 首次安装版长时间运行后本地 API 超时，页面停留在脚本清单加载；重启后 API、页面与取消恢复通过。启动日志管道或测试工具的影响尚未完全排除，需复测长期运行，不能宣称问题已根治。
- CDP 页面验证成功，但 Browser.close 的测试客户端没有正常返回；不把此过程算作原生托盘退出通过。
- 初次卸载时残留主 EXE 和 3 个 DLL；关闭挂起的 Playwright 诊断进程、恢复安装后，第二次卸载和重装退出码均为 0，主 EXE 实际删除后恢复，11 个数据文件的哈希保持一致。不是手动删除目录来代替卸载。
- 原生托盘图标、双击恢复、菜单退出尚未完成本轮操作验收。媒体库测试输出额外嵌套平台目录，扫描返回空，不能作为时间线和缩略图通过证据。

## S–V Release、哈希与正式产物

| 项目 | 结果 |
|---|---|
| S GitHub Release 地址 | 无本次 Release；[仓库 Releases](https://github.com/wiizZ82066/mediasqurrel/releases) 查询为空 |
| T 安装包 SHA256 | `25A5AEE2066B14AF2C48AFAEB791BBF907A2552DB8EE43D81BCE7528D6ACDB49` |
| U latest.yml SHA256 | `C85D7812216EE2FFC9BA5B3108C8E77C7EA71FDAF582AACDE7942E3C747D6F9B` |
| V 未签名正式产物 | 没有发布正式产物；本地 RC 安装包未签名，禁止冒充正式版 |

## W 退出与进程清理

真实 framework-smoke 的 app.quit 后，后端进程消失。安装版的异常检查和诊断进程另作测试清理；最终进程快照没有运行中的 Media Squirrel、MediaSquirrelBackend、Electron 或本仓库 release/backend-dist 路径下进程，release44 诊断会话也已关闭。用户原有的其他 Python 服务保持运行。强制清理不算“托盘正常退出 PASS”；本轮对崩溃、取消后所有进程树以及正常退出长时间稳定性的完整验证仍不足。

## 发布安全审计与下一步

- 实测 contextIsolation=true、nodeIntegration=false、sandbox=true；renderer 无 require，preload 只公开版本信息。后端实际监听 127.0.0.1，API/WS 使用当前动态端口。
- CSP 尚未配置；导航、新窗口缺少明确限制。未发现通过 preload 直接暴露任意 Node 执行的桥接，但不能因此宣称任意远端导航安全。
- 本地 HTTP/WS 没有完整 Host/Origin 和授权检查；路径前缀检查不能代替对 reparse/symlink 的真实路径约束。路径选择与目录访问仍需边界测试。
- 子进程使用参数数组；本次修复 stdin 继承阻塞。before-quit 没有等待树杀完成，崩溃看门狗不等于完整子进程树清理。
- CI 尚未启用可靠的 forceCodeSigning、后端签名、RFC3161/publisher/manifest 检查；Release 文件列表遗漏 blockmap；Secrets 被直接插入 shell 文本，应改为环境变量。现有流水线不能称为正式发布就绪。
- 本机构建不等于干净 CI：Python 与 CI 版本不同，依赖和浏览器安装的可重复性仍需实际 Actions 运行验收。没有触发正式 tag 或发布未签名资产。
- npm audit targeted 修复后 critical 为 0，仍有 8 项 moderate 构建链问题；未采用强制依赖降级。

先完成尚未通过的本机回归和异常定位，再补充真实签名凭据及稳定版官方 manifest 支持，执行两个真实签名 Releases 的升级闭环与干净 CI。全部门槛通过后才能更新为 v1.2.0、标记 READY FOR PUBLIC RELEASE，并按用户授权正常 push main。
