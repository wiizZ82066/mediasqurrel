"""配置中心：路径、端口等全局常量。"""
import os

# 项目根目录（<PROJECT_ROOT>）
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 脚本注册清单目录
MANIFEST_DIR = os.path.join(BASE_DIR, "scripts_manifest")

# 媒体库存档根目录（脚本默认输出位置）——暂时与项目同目录
LIBRARY_ROOT = BASE_DIR

# 数据库
DB_PATH = os.path.join(BASE_DIR, "app_data", "app.db")

# 服务
HOST = "127.0.0.1"
PORT = 8642

# 同时运行的下载任务数上限（浏览器爬虫不宜并发过多）
MAX_CONCURRENT_TASKS = 1
