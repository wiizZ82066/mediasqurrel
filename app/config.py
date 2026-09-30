"""配置中心：路径、端口等全局常量。

PyInstaller frozen 模式下的目录约定:
  - RESOURCE_DIR (sys._MEIPASS): 打包进 exe 的只读资源
      scripts_manifest/、下载脚本 .py、frontend/dist
  - BASE_DIR (exe 所在目录): 用户数据与输出
      媒体库存档、app_data/（数据库、缩略图缓存）
"""
import os
import sys

_FROZEN = getattr(sys, "frozen", False)

# exe 所在目录（frozen）或项目根目录；用户数据与媒体库的基准
BASE_DIR = os.path.dirname(sys.executable) if _FROZEN else os.path.dirname(
    os.path.dirname(os.path.abspath(__file__))
)

# 只读资源目录（frozen 时为 PyInstaller 解压目录）
RESOURCE_DIR = getattr(sys, "_MEIPASS", BASE_DIR)

# 脚本注册清单目录
MANIFEST_DIR = os.path.join(RESOURCE_DIR, "scripts_manifest")

# 媒体库存档根目录（脚本默认输出位置）
LIBRARY_ROOT = BASE_DIR

# 数据库
DB_PATH = os.path.join(BASE_DIR, "app_data", "app.db")

# 前端构建产物
FRONTEND_DIST = os.path.join(RESOURCE_DIR, "frontend", "dist")

# 服务
HOST = "127.0.0.1"
PORT = 8642

# 同时运行的下载任务数上限（浏览器爬虫不宜并发过多）
MAX_CONCURRENT_TASKS = 1
