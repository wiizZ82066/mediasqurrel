# 🐿️ Media Squirrel

本地媒体下载管理平台 —— 微博 / 抖音内容的一站式下载、订阅追踪与媒体库管理。

![tech](https://img.shields.io/badge/backend-FastAPI-009688) ![tech](https://img.shields.io/badge/frontend-Vue%203-42b883) ![tech](https://img.shields.io/badge/platform-Windows-0078D6)

## ✨ 功能特性

### 📥 脚本下载
- **微博**：正文 + 原图 + Live 图（mov + jpg 封面），优先官方 API、DOM 兜底
- **抖音**：无水印视频 + 官方封面，支持完整分享文案 / 短链 / 直链
- 插件式脚本注册（`scripts_manifest/*.json`），新增下载脚本零代码接入
- 内容有效性校验（防平台填错）、输出目录可视化选择 + 实时预览（图片堆叠轮播 / 视频播放器）
- 任务完成可直接预览输出或一键跳转媒体库对应位置

### 🔔 订阅追踪
- 博主订阅 + 定时扫描（默认 30 分钟），发现新内容自动通知 + 自动下载
- **博主搜索**：本地存档作者即时匹配 + 线上搜索（微博用户搜索 / 抖音半自动验证），点击即订阅、键盘 ↑↓ 选择回车确认
- **抖音登录态**：可见浏览器一次扫码，cookies 仅存本地（`app_data/`），扫描/下载自动注入
- **去重**：本地文件优先 + 数据库兜底——删档后重扫可重新下载，纯文字微博不漏
- 首次订阅仅对齐基线，不会批量回溯历史内容

### 🖼️ 媒体库
- 库级缓存 + 启动预热（毫秒级访问），下载完成自动失效，手动刷新兜底
- **人物优先封面算法**：YuNet 人脸检测（清晰度+分辨率评分），视频抽帧/官方封面/图片同台竞技，封面裁剪自动对准人脸
- **时间线视图**：竖线日期节点 + 横向卡片流；按作者网格视图
- Live 图悬停即播（微博式体验）、全文搜索、画廊浏览（键盘导航 + 相邻预加载）

### 🖥️ 桌面体验
- 系统托盘常驻（`python run.py`），关闭窗口后台继续扫描
- Apple 风格设计系统（[DESIGN.md](DESIGN.md)），全静态渲染零闪烁、回到顶部等细节交互

## 🚀 快速开始

### 环境要求

- Python 3.10+
- Node.js 18+（构建前端）
- Chrome / Chromium（下载脚本使用，playwright 以 `channel='chrome'` 调用系统 Chrome）

### 安装

```bash
# 1. Python 依赖
pip install -r requirements.txt
python -m playwright install chromium

# 2. 人脸检测模型（媒体库"人物优先封面"用，232KB，可选）
#    下载后放到 app_data/models/face_detection_yunet.onnx
#    https://huggingface.co/opencv/face_detection_yunet
#    未放模型时自动退化为清晰度算法

# 3. 构建前端
cd frontend
npm install
npm run build
cd ..
```

### 启动

```bash
python run.py
```

自动打开浏览器访问 `http://127.0.0.1:8642`，并驻留系统托盘。

开发模式：

```bash
python run.py --reload          # 后端热重载
cd frontend && npm run dev      # 前端 Vite (5173)
```

## 📁 目录结构

```
├── run.py                  # 启动器（托盘 + 浏览器）
├── app/                    # FastAPI 后端
│   ├── main.py             #   路由 + WS + 静态托管
│   ├── task_manager.py     #   任务队列 + 状态机 + 实时日志
│   ├── script_registry.py  #   插件式脚本注册
│   ├── media_library.py    #   存档扫描
│   ├── watcher.py          #   订阅调度
│   ├── thumbs.py           #   缩略图引擎
│   └── scanners/           #   平台扫描器（微博/抖音）
├── scripts_manifest/       # 脚本清单（新脚本接入点）
├── frontend/               # Vue 3 前端
├── weibo_downloader.py     # 微博下载脚本
├── douyin_downloader.py    # 抖音下载脚本
└── DESIGN.md               # 设计规范
```

## 🔌 新增下载脚本

1. 脚本放到根目录（如 `xxx_downloader.py`）
2. 在 `scripts_manifest/` 新建清单：

```json
{
  "id": "xxx",
  "name": "XXX 下载器",
  "icon": "📦",
  "script": "xxx_downloader.py",
  "params": [
    { "name": "url", "label": "链接", "kind": "text", "required": true, "flag": "-url" }
  ]
}
```

3. 在 `.gitignore` 放行区加一行 `!/xxx_downloader.py`

UI 自动出现新表单，无需改任何后端代码。

## 🤖 AI 一键部署

不想手动操作？把下面的指令区块**整段复制**给任何 AI 编程助手（Claude Code / Cursor / Copilot / ChatGPT 等），把本项目代码放在它的可访问目录，它就能自动完成部署：

````markdown
请帮我部署 Media Squirrel（本地媒体下载管理平台），按以下步骤执行：

## 环境要求
- Windows / macOS / Linux（下载功能依赖 Chrome 浏览器）
- Python 3.10+
- Node.js 18+

## 部署步骤（按顺序执行，每步失败需停下排查）

1. 进入项目根目录，安装 Python 依赖：
   pip install -r requirements.txt

2. 安装 Playwright 浏览器内核：
   python -m playwright install chromium
   （需要系统已安装 Chrome；下载脚本会以 channel='chrome' 调用系统 Chrome）

3. 下载人脸检测模型（媒体库"人物优先封面"功能，232KB，可选但推荐）：
   创建目录 app_data/models/，下载以下文件并保存为
   app_data/models/face_detection_yunet.onnx：
   https://huggingface.co/opencv/face_detection_yunet/resolve/main/face_detection_yunet_2023mar.onnx
   （下载失败可跳过，功能自动退化为清晰度算法）

4. 构建前端：
   cd frontend && npm install && npm run build && cd ..
   （必须先构建，后端会托管 frontend/dist）

5. 启动服务：
   python run.py
   预期：自动打开浏览器访问 http://127.0.0.1:8642，
   系统托盘出现松鼠图标。

## 验证清单（全部通过才算部署成功）
- [ ] 首页加载并显示"下载内容"页面，能看到抖音/微博两个下载器卡片
- [ ] 任务页 / 媒体库页 / 订阅页可点击切换且正常渲染
- [ ] 粘贴一条抖音分享文案到下载页能创建任务并出现在任务队列
- [ ] 媒体库能扫描到已有存档目录（<作者>/<日期>/ 结构）

## 常见问题
- 端口占用：python run.py --port 9000 换端口
- 下载脚本被风控：任务日志会显示接口 403，稍后重试即可
- 抖音线上搜索需登录：订阅页点击"登录抖音"，在弹出的浏览器窗口完成
  滑块验证+扫码，登录态只保存在本地 app_data/douyin_cookies.json
````

> 💡 提示：AI 助手执行时如遇网络问题（pip/npm/HuggingFace），提醒它配置镜像源或代理。

## ⚠️ 免责声明

本项目仅供**个人学习与内容备份**用途。使用者需遵守目标平台的服务条款及当地法律法规，对使用本工具产生的任何后果自行负责。请勿用于商业用途或侵犯他人权益。

## License

MIT
