"""配置中心：路径、端口等全局常量。

目录策略（三态）:
  源码运行:  BASE_DIR = 项目根（开发体验不变）
  桌面打包:  BASE_DIR = %LOCALAPPDATA%/Media Squirrel（用户可写数据）
             RESOURCE_DIR = PyInstaller 解压目录（只读资源）
  环境覆盖:  MS_DATA_DIR 环境变量优先（测试/便携模式）

资源（只读，随包分发）:   用户数据（可写，LOCALAPPDATA）:
  scripts_manifest/          app_data/（SQLite、缩略图、cookie、封面缓存）
  weibo/douyin_downloader    library/（媒体库存档，用户可在下载页另选）
  frontend/dist              app_data/douyin_profile（登录浏览器档案）
  app_data/models（人脸模型）
"""
import os
import sys

_FROZEN = getattr(sys, "frozen", False)


def _resolve_base_dir() -> str:
    # 1. 环境变量覆盖（测试/便携模式）
    env = os.environ.get("MS_DATA_DIR")
    if env:
        return os.path.abspath(env)
    if _FROZEN:
        # 2. 桌面应用：用户可写数据目录（不能写 Program Files）
        local = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
        return os.path.join(local, "Media Squirrel")
    # 3. 源码运行：项目根
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


BASE_DIR = _resolve_base_dir()

# 只读资源目录（frozen 时为 PyInstaller 解压目录）
RESOURCE_DIR = getattr(sys, "_MEIPASS", BASE_DIR)

# 脚本注册清单目录
MANIFEST_DIR = os.path.join(RESOURCE_DIR, "scripts_manifest")

# 媒体库存档根目录（脚本默认输出位置；下载页可改）
LIBRARY_ROOT = BASE_DIR if not _FROZEN else os.path.join(BASE_DIR, "library")

# frozen/自定义数据目录时确保关键可写目录存在
# （StaticFiles 挂载要求目录存在；源码运行时项目根必然存在）
if _FROZEN or os.environ.get("MS_DATA_DIR"):
    os.makedirs(LIBRARY_ROOT, exist_ok=True)
    os.makedirs(os.path.join(BASE_DIR, "app_data"), exist_ok=True)

# 数据库
DB_PATH = os.path.join(BASE_DIR, "app_data", "app.db")

# 前端构建产物
FRONTEND_DIST = os.path.join(RESOURCE_DIR, "frontend", "dist")

# 人脸检测模型（随包分发的只读资源）
FACE_MODEL_PATH = os.path.join(RESOURCE_DIR, "app_data", "models", "face_detection_yunet.onnx")

# 服务
HOST = "127.0.0.1"
PORT = int(os.environ.get("MS_PORT") or 8642)

# 同时运行的下载任务数上限（浏览器爬虫不宜并发过多）
MAX_CONCURRENT_TASKS = 1
