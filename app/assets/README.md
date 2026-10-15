# 托盘图标

这些 PNG 复用项目已有 SVG，供 Pillow 和 Windows 托盘菜单直接加载，不增加 SVG 渲染运行依赖，也未引入新的第三方素材。

| 资产 | 来源 | 尺寸与处理 |
| --- | --- | --- |
| `squirrel-tray.png` | [`frontend/public/squirrel.svg`](../../frontend/public/squirrel.svg) | 256 × 256 透明 PNG，保留原图形与配色 |
| `tray/download.png`、`library.png`、`tasks.png`、`subs.png`、`settings.png`、`folder.png` | [`frontend/src/components/AppIcon.vue`](../../frontend/src/components/AppIcon.vue) 对应的图标分支 | 64 × 64 透明 PNG，保留原图形与配色 |
| `tray/close.png` | 同一组件的 `close` 分支 | 64 × 64 透明 PNG，将描边从 `#758394` 改为退出操作使用的红色 `#ff3b30` |

生成方式：使用离线 Chromium canvas 渲染原 SVG（主图标 1024 × 1024，菜单图标 256 × 256），再用 Pillow 的 `Image.Resampling.LANCZOS` 缩放到交付尺寸。SVG 修改后应同步重新生成对应 PNG；正常启动仅加载 PNG，无需浏览器渲染。
