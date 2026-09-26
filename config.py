from pathlib import Path
import shutil
import yaml

BASE_DIR = Path("/app/quest")
TXT_DIR = BASE_DIR / "txt"
PIC_DIR = BASE_DIR / "pic"
FILE_DIR = BASE_DIR / "file"
ALL_DIR = BASE_DIR / "all"
CACHE_DIR = BASE_DIR / "cache"
META_PATH = BASE_DIR / "meta.yaml"
ERROR_PATH = BASE_DIR / "error.jsonl"
CHECKPOINT_PATH = CACHE_DIR / "checkpoint.json"
PENDING_PATH = CACHE_DIR / "pending.jsonl"
INDEX_PATH = CACHE_DIR / "index.json"

# 插件目录内的模板 meta.yaml（首次运行拷贝到 /app/quest）
PLUGIN_META = Path(__file__).parent / "meta.yaml"

for d in [TXT_DIR, PIC_DIR, FILE_DIR, ALL_DIR, CACHE_DIR]:
    d.mkdir(parents=True, exist_ok=True)

DEFAULT_META = {
    "target_users": ["302771504"],
    "blacklist": {
        "enabled": False,
        "prefixes": ["管理后台", "后台管理", "喜报", "悲报"],
    },
    "txt_max_size_kb": 500,
    "forward_max_depth": 2,
    "save_all": True,
    "catch_up_max_count": 200,
    "startup_wait_sec": 60,
    "fetch_interval_sec": 30,
    "fetch_page_size": 20,
    "japanese_ratio": 0.3,
    "download_concurrency": 1,
}


def deep_merge(base: dict, override: dict) -> dict:
    """递归合并，override 优先。用于 meta.yaml 只写部分字段的场景。"""
    result = dict(base)
    for k, v in override.items():
        if k in result and isinstance(result[k], dict) and isinstance(v, dict):
            result[k] = deep_merge(result[k], v)
        else:
            result[k] = v
    return result


def load_meta() -> dict:
    # 首次运行：把插件目录里的 meta.yaml 拷到 /app/quest
    if not META_PATH.exists() and PLUGIN_META.exists():
        shutil.copy(PLUGIN_META, META_PATH)

    if META_PATH.exists():
        with open(META_PATH, "r", encoding="utf-8") as f:
            loaded = yaml.safe_load(f) or {}
        return deep_merge(DEFAULT_META, loaded)

    with open(META_PATH, "w", encoding="utf-8") as f:
        yaml.dump(DEFAULT_META, f, allow_unicode=True, sort_keys=False)
    return DEFAULT_META


META = load_meta()
