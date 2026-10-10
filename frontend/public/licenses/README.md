# 图标来源与许可

界面图标核查日期：2026-10-09。以下界面图标本地随应用分发，不访问远程图标 CDN。

- `AppIcon.vue` 的松鼠品牌及普通操作图标：本项目原创 SVG，随项目许可分发。
- 扫描图标：ByteDance IconPark **Scanning**（非 FaceScan）。来源 https://github.com/bytedance/IconPark/blob/master/packages/svg/src/icons/Scanning.ts ，Copyright 2019-present Bytedance Inc.，Apache-2.0。仅将运行时组件改写为内联 SVG 并设置静态蓝绿颜色，路径不变。完整许可：`iconpark-apache-2.0.txt`。原仓库已于 2025-12-08 归档；不引入运行时依赖。
- 微博标识：Simple Icons `sinaweibo.svg`，来源 https://github.com/simple-icons/simple-icons/blob/develop/icons/sinaweibo.svg 。下载文件 SHA-256：`44facf47c6bfa06c312c28acda6aa2ed404ef7f68c2897c8f33ad51b6ba94059`。SVG 路径未修改；界面单色着色。
- 抖音音乐标识：Simple Icons `tiktok.svg`，来源 https://github.com/simple-icons/simple-icons/blob/develop/icons/tiktok.svg 。下载文件 SHA-256：`6f54ac8d325faacea8935bdc44cbed60206a6b408641799e5fea1cba7c1a0af7`。用于界面识别抖音下载平台，SVG 路径未修改。

Simple Icons 项目采用 CC0-1.0（完整文本：`simple-icons-cc0.txt`），其免责声明说明项目许可不代表所有品牌图形均获商标授权；原声明保留于 `simple-icons-disclaimer.md`。微博、抖音／TikTok 商标归各自权利人所有。平台标识仅用于说明互操作平台，不表示官方授权、关联或背书。本项目原创松鼠与第三方平台标识分别使用。

## 正文中的微博表情

这些是所查看内容的表情，不属于本项目的操作图标。`app/weibo_emoticons.json` 保存 2026-10-10 核验的 338 项名称与静态资源地址：337 项来自微博官方公开配置 https://weibo.com/ajax/statuses/config ，另一个 `[指定能行]` 由官方公开帖子响应 https://weibo.com/ajax/statuses/show?id=PvSWpaoG9 中明确配对的图片补充。图片来源为 `https://face.t.sinajs.cn/t4/appstyle/expression/ext/normal/`。图片版权属于各自权利人，不声明为开源或可自由商用，也不随仓库分发。

本机服务仅在显示已收录表情时读取明确映射的官方 PNG 地址，缓存透明静态图像供后续浏览；不提交缓存、不读取登录态、不把正文发给第三方。未知名称和加载失败均保留原始表情代码。
