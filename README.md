# 🐿️ Media Squirrel

本地媒体下载管理平台 —— 微博 / 抖音内容的一站式下载、订阅追踪与媒体库管理。

![tech](https://img.shields.io/badge/backend-FastAPI-009688) ![tech](https://img.shields.io/badge/frontend-Vue%203-42b883) ![tech](https://img.shields.io/badge/platform-Windows-0078D6)

## ✨ 功能特性

### 📥 脚本下载
- **微博**：正文 + 原图 + Live 图（mov + jpg 封面），优先官方 API、DOM 兜底
- **抖音**：无水印视频，支持完整分享文案 / 短链 / 直链
- 插件式脚本注册（`scripts_manifest/*.json`），新增下载脚本零代码接入
- 粘贴文案自动识别平台，输出目录可视化选择 + 实时预览（图片堆叠轮播 / 视频播放器）

### 🔔 订阅追踪
- 博主订阅 + 定时扫描（默认 30 分钟），发现新内容自动下载
- 首次订阅仅对齐基线，不会批量回溯历史内容
- 页面内通知中心（发现新内容 / 下载完成），无需系统通知

### 🖼️ 媒体库
- 自动扫描存档生成时间线画廊（`<作者>/<日期>/` 目录约定）
- 缩略图引擎：图片压缩 / 视频抽帧 / 中文路径兼容 / 磁盘缓存
- Live 图悬停即播（微博式体验）
- 全文搜索（作者 / 日期 / 标题 / 正文）、按作者 / 时间线双视图、排序筛选

### 🖥️ 桌面体验
- 系统托盘常驻（`python run.py`），关闭窗口后台继续扫描
- Apple 风格设计系统（[DESIGN.md](DESIGN.md)），全静态渲染零闪烁

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
├── src-tauri/              # Tauri 桌面壳（实验性）
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

## ⚠️ 免责声明

本项目仅供**个人学习与内容备份**用途。使用者需遵守目标平台的服务条款及当地法律法规，对使用本工具产生的任何后果自行负责。请勿用于商业用途或侵犯他人权益。

## License

MIT
