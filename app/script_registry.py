"""脚本注册器：扫描 scripts_manifest/*.json，提供清单查询与命令拼接。

新脚本接入步骤：
  1. 把脚本 .py 放到项目根目录
  2. 在 scripts_manifest/ 写一个 manifest json
无需改任何后端代码，UI 会自动出现新脚本的表单。
"""
import json
import os
import sys
from typing import Optional

from . import config


def _load_all() -> dict:
    manifests = {}
    if not os.path.isdir(config.MANIFEST_DIR):
        return manifests
    for fn in os.listdir(config.MANIFEST_DIR):
        if not fn.endswith(".json"):
            continue
        path = os.path.join(config.MANIFEST_DIR, fn)
        try:
            with open(path, "r", encoding="utf-8") as f:
                m = json.load(f)
            # 校验必备字段
            if not (m.get("id") and m.get("script")):
                continue
            m["_script_path"] = os.path.join(config.BASE_DIR, m["script"])
            m["_available"] = os.path.isfile(m["_script_path"])
            manifests[m["id"]] = m
        except Exception as e:
            print(f"[registry] 清单加载失败 {fn}: {e}")
    return manifests


_CACHE: Optional[dict] = None


def reload():
    global _CACHE
    _CACHE = None


def all_scripts() -> list[dict]:
    """返回全部清单（剔除内部字段）。"""
    global _CACHE
    if _CACHE is None:
        _CACHE = _load_all()
    return [dict(m) for m in _CACHE.values()]


def get(script_id: str) -> Optional[dict]:
    global _CACHE
    if _CACHE is None:
        _CACHE = _load_all()
    m = _CACHE.get(script_id)
    return dict(m) if m else None


def build_command(script_id: str, params: dict) -> list[str]:
    """根据清单 + 用户参数拼出完整命令行（argv 列表）。

    - positional 参数：直接追加（含空格原样保留）
    - flag 参数：[flag, value]；kind=switch 且为真时只追加 flag
    """
    m = get(script_id)
    if not m:
        raise ValueError(f"未知脚本: {script_id}")
    if not m["_available"]:
        raise ValueError(f"脚本文件不存在: {m['script']}")

    argv = [sys.executable, "-u", m["_script_path"]]
    for p in m.get("params", []):
        name = p["name"]
        if name not in params:
            continue
        val = params[name]
        if p.get("positional"):
            sval = str(val).strip()
            if sval:
                argv.append(sval)
            continue
        flag = p.get("flag")
        if not flag:
            continue
        if p.get("kind") == "switch":
            if val in (True, "true", "True", 1, "1"):
                argv.append(flag)
        else:
            sval = str(val).strip()
            if sval:
                argv.extend([flag, sval])
    return argv
