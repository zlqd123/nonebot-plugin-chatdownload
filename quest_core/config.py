"""运行时配置与路径 —— 由 plugin.py 在 on_load / on_config_update 时注入。

MaiBot 新版 SDK 的插件运行在独立 Runner 子进程里，无法访问
/app/quest 这类硬编码路径，改用 Host 授予的 data_dir。
"""

import json
from pathlib import Path

DEFAULTS = {
    "target_users": [],
    "save_dir": "",
    "txt_max_lines": 100,
    "save_all": True,
    "catch_up_max_count": 200,
    "catch_up_max_days": 14,
    "startup_wait_sec": 60,
    "fetch_interval_sec": 30,
    "fetch_page_size": 20,
    "japanese_ratio": 0.3,
    "download_concurrency": 1,
    "classify_txt": False,
    "blacklist_enabled": False,
    "blacklist_prefixes": [],
}

_cfg: dict = dict(DEFAULTS)
_base_dir: Path = Path(".")  # 占位，on_load 时被 set_base_dir 覆盖

# config.XXX -> data 目录下的相对路径
_SUBDIRS = {
    "TXT_DIR": "txt",
    "PIC_DIR": "pic",
    "FILE_DIR": "file",
    "ALL_DIR": "all",
    "CACHE_DIR": "cache",
    "CHECKPOINT_PATH": "cache/checkpoint.json",
    "PENDING_PATH": "cache/pending.jsonl",
    "INDEX_PATH": "cache/index.json",
    "ERROR_PATH": "error.jsonl",
}


def _normalize_users(val) -> list[str]:
    """容错归一化 target_users：
    - WebUI 里可能把 ["123"] 整串填成一项 → 自动展开
    - 单个字符串 → 包成列表
    """
    if isinstance(val, str):
        val = [val]
    out: list[str] = []
    for u in val or []:
        u = str(u).strip()
        if u.startswith("[") and u.endswith("]"):
            try:
                inner = json.loads(u)
                if isinstance(inner, list):
                    out.extend(str(x).strip() for x in inner)
                    continue
            except Exception:
                pass
        if u:
            out.append(u)
    return out


def set_config(cfg: dict):
    """用配置模型 dump 出来的 dict 覆盖运行时配置（只接受已知键）。"""
    for k in DEFAULTS:
        if k in cfg and cfg[k] is not None:
            _cfg[k] = cfg[k]
    if "target_users" in cfg and cfg["target_users"] is not None:
        _cfg["target_users"] = _normalize_users(cfg["target_users"])


def get(key: str):
    return _cfg.get(key, DEFAULTS.get(key))


def set_base_dir(data_dir, override_dir: str = ""):
    """以 Host 授予的插件数据目录为根，创建归档子目录。

    配置了 save_dir 时用 save_dir 覆盖（用户自选保存路径）。
    """
    global _base_dir
    _base_dir = Path(override_dir) if override_dir else Path(data_dir)
    # 注意：模块内部不能用 TXT_DIR 这类裸名（__getattr__ 只拦截外部属性访问）
    for name in ("txt", "pic", "file", "all", "cache"):
        (_base_dir / name).mkdir(parents=True, exist_ok=True)
    # 自愈：旧版本 bug 曾把 error.jsonl 建成目录，log_error 会因此永远失败
    err = _base_dir / "error.jsonl"
    if err.is_dir():
        try:
            err.rmdir()
        except OSError:
            pass


def __getattr__(name: str):
    if name == "BASE_DIR":
        return _base_dir
    if name in _SUBDIRS:
        return _base_dir / _SUBDIRS[name]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
