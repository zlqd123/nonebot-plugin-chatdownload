import asyncio
import json

from nonebot import get_driver, logger
from nonebot.adapters.onebot.v11 import Bot

from .config import (
    META, CHECKPOINT_PATH, INDEX_PATH, PENDING_PATH,
    TXT_DIR, PIC_DIR, FILE_DIR,
)
from .storage import (
    load_index, get_index, add_index, write_all,
    read_checkpoint, write_checkpoint, delete_checkpoint,
    read_pending, delete_pending, append_text, log_error,
)
from .classifier import get_category
from .downloader import download_file, ensure_ext

driver = get_driver()
_catch_up_done = False


# 补抓期间实时消息的落盘开关。
# 用 dict 包装 + 函数访问：避免 `from .history import is_catching_up`
# 按值复制布尔量、导致 monitor 永远读到 False 的陷阱。
_state = {"is_catching_up": False}


def set_catching_up(value: bool):
    _state["is_catching_up"] = value


def is_catching_up() -> bool:
    return _state["is_catching_up"]


# ============ 合并消息展开 ============
async def expand_forward(bot: Bot, message: dict) -> list[dict]:
    """展开 message 中的 forward 段（只展开一层），返回归一化后的子消息列表。

    gocq/NapCat 的节点结构是 {content: [...], sender, time}，
    统一补上 "message" 字段后再交给 save_message 递归处理。
    """
    collected: list[dict] = []
    for seg in message.get("message", []):
        if not isinstance(seg, dict) or seg.get("type") != "forward":
            continue
        forward_id = seg.get("data", {}).get("id")
        if not forward_id:
            continue
        try:
            result = await bot.call_api("get_forward_msg", id=forward_id)
            data = result.get("data", result) if isinstance(result, dict) else {}
            sub_messages = data.get("messages", []) if isinstance(data, dict) else []
            for sub in sub_messages:
                if not isinstance(sub, dict):
                    continue
                # 原始节点进 all 存档
                write_all(sub)
                collected.append({
                    "message_id": sub.get("message_id"),
                    "message": sub.get("content") or sub.get("message") or [],
                    "time": sub.get("time"),
                    "sender": sub.get("sender"),
                })
        except Exception as e:
            logger.warning(f"[quest] 展开合并消息失败: {e}")
    return collected


# ============ 单条消息保存（幂等） ============
async def save_message(bot: Bot, message: dict, depth: int = 0):
    message_id = message.get("message_id")

    # 幂等：已保存过就直接跳过。
    # forward 子消息多数没有 message_id，去重锚点是父消息——
    # 父消息进过 index，就代表它（含展开的子消息）已处理过。
    if message_id is not None and message_id in get_index():
        return

    for seg in message.get("message", []):
        if not isinstance(seg, dict):
            continue
        seg_type = seg.get("type")
        data = seg.get("data", {})

        if seg_type == "text":
            text = data.get("text", "")
            # 黑名单（保留逻辑，默认关闭）
            # if META["blacklist"]["enabled"] and any(
            #     text.startswith(p) for p in META["blacklist"]["prefixes"]
            # ):
            #     continue
            if text.strip():
                append_text(get_category(text), text.replace("\n", "\\n"))

        elif seg_type == "image":
            url = data.get("url")
            filename = ensure_ext(data.get("file") or f"{message_id}.jpg", ".jpg")
            if url:
                await download_file(url, PIC_DIR, filename, message_id, "image")
            else:
                log_error(message_id, "", "image", "no url in image segment")

        elif seg_type == "record":
            # QQ 原生语音不下载
            pass

        elif seg_type == "file":
            url = data.get("url")
            filename = ensure_ext(data.get("file") or f"{message_id}.bin", ".bin")
            if url:
                await download_file(url, FILE_DIR, filename, message_id, "file")
            else:
                log_error(message_id, "", "file", "no url in file segment")

        elif seg_type == "video":
            url = data.get("url")
            filename = ensure_ext(data.get("file") or f"{message_id}.mp4", ".mp4")
            if url:
                await download_file(url, FILE_DIR, filename, message_id, "video")
            else:
                log_error(message_id, "", "video", "no url in video segment")

    # 合并转发：展开一层并递归保存子消息（含展开时的 all 存档）
    if depth < META["forward_max_depth"]:
        for sub in await expand_forward(bot, message):
            await save_message(bot, sub, depth + 1)

    if message_id is not None:
        add_index(message_id)


# ============ 历史抓取 ============
async def fetch_history(bot: Bot, user_id: str, anchor: int) -> list:
    result = await bot.call_api(
        "get_friend_msg_history",
        user_id=user_id,
        message_seq=str(anchor),  # NapCat 文档要求 string；若翻页失效改回 int 并测 reverseOrder 参数名
        count=META["fetch_page_size"],
        reverse_order=True,
    )
    data = result.get("data", result) if isinstance(result, dict) else {}
    return data.get("messages", []) if isinstance(data, dict) else []


async def catch_up(bot: Bot, user_id: str):
    set_catching_up(True)
    logger.info(f"[quest] 开始补抓私聊历史，用户 {user_id}")

    hit_boundary = False
    try:
        load_index()  # 防御：确保 index 已加载

        cp = read_checkpoint()
        if cp and cp.get("user_id") == user_id:
            anchor = cp.get("last_message_seq", 0)
            logger.info(f"[quest] 从存档点继续: {cp}")
        else:
            if cp:
                logger.warning(f"[quest] 存档点属于其他用户，忽略: {cp.get('user_id')}")
            anchor = 0

        total = 0
        max_count = META["catch_up_max_count"]
        prev_anchor = None

        while total < max_count:
            try:
                messages = await fetch_history(bot, user_id, anchor)
            except Exception as e:
                logger.error(f"[quest] 历史抓取失败: {e}")
                break

            if not messages:
                # 历史到头
                hit_boundary = True
                break

            oldest = min(messages, key=lambda m: m.get("time", 0))
            new_anchor = oldest.get("message_seq", 0)

            # 边界快照：必须在保存之前计算——保存后整页都会进入 index
            page_all_old = all(
                m.get("message_id") in get_index() for m in messages
            )

            # 只处理不在 index 里的消息（幂等，避免重复写 all/txt/图片）
            for msg in messages:
                if msg.get("message_id") in get_index():
                    continue
                write_all(msg)
                await save_message(bot, msg)

            total += len(messages)

            # 护栏：anchor 不前进说明翻页失效（如 message_seq 缺失/参数被忽略）
            if new_anchor == prev_anchor:
                logger.warning("[quest] anchor 未前进，停止补抓（翻页可能失效）")
                break
            prev_anchor = new_anchor

            if page_all_old:
                hit_boundary = True
                logger.info("[quest] 整页已缓存，追上边界，停止补抓")
                break

            anchor = new_anchor
            write_checkpoint(
                user_id=user_id,
                message_seq=anchor,
                message_id=oldest.get("message_id", 0),
                time=oldest.get("time", 0),
            )

            if total >= max_count:
                logger.info(f"[quest] 达到最大条数 {max_count}，停止")
                break

            await asyncio.sleep(META["fetch_interval_sec"])
    finally:
        # 无论成功/异常，开关必须复位
        set_catching_up(False)
        logger.info("[quest] 补抓结束")

    # 只在真正追上边界/历史到头时删存档点；异常、达上限、翻页失效都保留。
    # 且只删属于当前用户的存档点（多用户轮转场景）。
    if hit_boundary:
        cp_now = read_checkpoint()
        if cp_now is None or cp_now.get("user_id") == user_id:
            delete_checkpoint()
        else:
            logger.info(f"[quest] 存档点属于其他用户 {cp_now.get('user_id')}，保留")
    else:
        logger.info("[quest] 保留存档点，下次启动继续")


async def process_pending(bot: Bot):
    """处理补抓期间实时落盘的新消息"""
    items = read_pending()
    if not items:
        return
    logger.info(f"[quest] 处理 pending: {len(items)} 条")
    failed = []
    for msg in items:
        try:
            await save_message(bot, msg)
        except Exception as e:
            logger.error(f"[quest] pending 处理失败: {e}")
            failed.append(msg)

    if failed:
        # 失败项原样保留，下次启动重试
        with open(PENDING_PATH, "w", encoding="utf-8") as f:
            for msg in failed:
                f.write(json.dumps(msg, ensure_ascii=False) + "\n")
        logger.warning(f"[quest] {len(failed)} 条 pending 处理失败，保留待重试")
    else:
        delete_pending()
        logger.info("[quest] pending 处理完成")


@driver.on_startup
async def on_startup():
    load_index()
    logger.info("[quest] 插件启动")


@driver.on_bot_connect
async def on_bot_connect(bot: Bot):
    global _catch_up_done
    if _catch_up_done:
        return
    _catch_up_done = True

    await asyncio.sleep(META["startup_wait_sec"])

    has_data = (
        any(TXT_DIR.iterdir())
        or any(PIC_DIR.iterdir())
        or any(FILE_DIR.iterdir())
        or INDEX_PATH.exists()
    )
    has_checkpoint = CHECKPOINT_PATH.exists()

    if has_data or has_checkpoint:
        for user_id in META["target_users"]:
            try:
                await catch_up(bot, user_id)
            except Exception as e:
                # 单个用户失败不阻断其余用户与 pending 处理
                logger.error(f"[quest] 补抓异常 {user_id}: {e}")

    # 先补抓，再处理 pending
    await process_pending(bot)
