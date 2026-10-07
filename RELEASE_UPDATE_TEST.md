# 自动更新验收记录

日期：2026-10-07。起始代码：`541debcda326421a3ae274f5edafdc437a786d36`。

当前结论：**RELEASE CANDIDATE**。尚未完成真实 GitHub Releases 两版本升级闭环。

## 已核查事实

- `gh release list --repo wiizZ82066/mediasqurrel --limit 10` 成功返回空列表。
- `gh secret list --repo wiizZ82066/mediasqurrel` 成功返回空列表。仅检查名称，没有读取或输出 Secret 值。
- npm 官方 registry：electron-builder `latest=26.15.3`、`v26=26.17.0`、`next=27.0.0-alpha.9`；electron-updater `latest=6.8.9`、`v26=6.8.10`、`next=7.0.0-alpha.8`。
- [官方 next 配置文档](https://www.electron.build/docs/configuration/) 明确注明 v27 尚未发布。官方 `updateManifest` 配置不能直接加入当前 v26 schema。
- 本地 `app-builder-lib/scheme.json` 不包含 `updateManifest`；另从 npm 下载 `app-builder-lib@26.17.0` tarball，扫描其 JSON、JS、类型声明，同样没有 `updateManifest` / `Ed25519`。当前 updater 的 `parseUpdateInfo()` 只解析 YAML。
- 对当前 `release/latest.yml` 实际调用 `parseUpdateInfo()`：无 `signature` 被接受，追加 `signature: invalid` 仍被接受。此为真实库解析器检查，**不是已完成客户端端到端安全测试**。
- 旧 `release/win-unpacked/resources/app-update.yml` 仅包含 GitHub provider 和缓存目录，没有可信清单公钥，也没有 `publisherName`。

## 必测矩阵

| 场景 | 本次结果 | 原因 / 证据 |
|---|---|---|
| 旧版发现 GitHub 新版 | NOT TESTED | 没有两个可用的签名 Releases |
| 下载新版与 blockmap | NOT TESTED | 同上 |
| 校验、安装、重启到新版 | NOT TESTED | 同上 |
| SQLite、Cookies、媒体库配置、下载目录保留 | NOT TESTED | 尚未进行真实安装升级；本地隔离测试不能代替 |
| 不产生第二套正式用户数据 | NOT TESTED | 同上 |
| 更新下载期间运行下载任务 | NOT TESTED | 无真实更新目标 |
| 任务完成后允许安装 | NOT TESTED | 同上 |
| GitHub 不可访问时应用继续工作 | NOT TESTED | 尚未完成打包客户端故障注入 |
| GitHub 没有已发布版本 | PASS（限定场景） | 安装版实际记录 `No published versions on GitHub`，随后两个真实下载成功；不代表断网、损坏包等其他失败场景通过 |
| latest.yml 缺失 | NOT TESTED | 同上 |
| 更新下载失败 | NOT TESTED | 同上 |
| 哈希校验失败 / 损坏安装包 | NOT TESTED | 同上 |
| 无清单签名应拒绝 | FAIL（实现审计） | 当前解析器接受无签名；官方方案尚未接入 |
| 被篡改签名应拒绝 | FAIL（实现审计） | 当前解析器接受无效 signature |
| 错误 key 应拒绝 | NOT TESTED | 尚无可用的官方清单验证器及可信 key 配置 |
| 合法签名允许下载 | NOT TESTED | 同上 |

## 待修复的发布阻塞

1. `autoInstallOnAppQuit=true` 绕过手动安装时的任务检查。
2. 任务接口错误、超时、JSON 解析失败返回 false，会把未知状态当作空闲。
3. 需要明确显示运行任务数量并提供“稍后”；任务状态与安装之间还需要防止新任务进入的竞态。
4. 当前清单签名注释声称 fail-closed，但实现不支持，不能据此宣称安全。
5. 缺少 Windows 证书、publisher、RFC3161 时间戳实测及三份 EXE 的签名验证。
6. 工作流只发布 Setup 和 latest.yml，漏掉 blockmap；未验证 manifest、backend 签名和强制签名配置。

用户要求先完成 Electron 全部前置回归再推进自动更新。本记录不将审计、配置或本地模拟写成更新闭环 PASS，不发布无签名正式产物，不使用 alpha 替代稳定发布依赖。
