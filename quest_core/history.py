"""历史补抓、消息归一化与保存 —— 适配 MaiBot 版 history.py。

与原 nonebot 版的关键差异：
- 原版通过 OneBot 协议端 API（get_friend_msg_history）向前翻页；
  MaiBot 插件拿不到协议端 API，改为从 Host 消息数据库补抓
  （ctx.chat.get_stream_by_user_id + ctx.message.get_recent /
   ctx.message.get_by_time_in_chat），边界推进用"最旧消息时间"做锚点。
- 合并转发无法调用 get_forward_msg 展开（没有协议端通道），
  forward 段只记入 all 日志，不再递归展开。
- 消息段里的图片/文件可能来自 DB 记录，只有纯文本没有 URL，
  补抓部分尽力而为；实时消息则与原版能力一致。
"""

import asyncio
import json
import time

from . import classifier, config, downloader, storage

logger = None  # 由 plugin.py 注入（ctx.logger）


def set_logger(l):
    global logger
    logger = l
    downloader.logger = l


# 补抓期间实时消息的落盘开关。
# 用 dict 包装 + 函数访问：避免 `from .history import is_catching_up`
# 按值复制布尔量、导致 monitor 永远读到 False 的陷阱。
_state = {"is_catching_up": False}


def set_catching_up(value: bool):
    _state["is_catching_up"] = value


def is_catching_up() -> bool:
    return _state["is_catching_up"]


# ============ 消息归一化 ============

def _normalize_ts(ts) -> float:
    """时间戳统一成秒。兼容 int/float（秒或毫秒）与字符串形式。"""
    if isinstance(ts, str):
        try:
            ts = float(ts)
        except ValueError:
            return 0.0
    if isinstance(ts, (int, float)) and ts > 0:
        return ts / 1000 if ts > 1e12 else ts
    return 0.0


def _seg_to_dict(seg) -> dict | None:
    """消息段统一转成 {type, data: dict}。

    兼容三种形态：
    - Hook 载荷 dict：text 段的 data 是纯字符串；
      image/emoji 段的二进制在段顶层 binary_data_base64
    - maim_message Seg dict：{type, data}
    - SDK MessageSegment 对象：.type / .data
    """
    if isinstance(seg, dict):
        t = seg.get("type", "text")
        d = seg.get("data", {})
        b64 = seg.get("binary_data_base64")
    else:
        t = getattr(seg, "type", None)
        if t is None:
            return None
        d = getattr(seg, "data", {})
        b64 = None
    if isinstance(d, str):
        if t == "text":
            d = {"text": d}
        elif d.startswith(("http://", "https://", "file://")):
            d = {"url": d}
        else:
            d = {"content": d}
    elif not isinstance(d, dict):
        d = {}
    if b64 and "base64" not in d:
        d["base64"] = b64
    return {"type": t, "data": d}


def normalize_hook_message(message: dict) -> dict | None:
    """把 chat.receive.after_process Hook 的 message 载荷归一化。

    载荷结构（src/plugin_runtime/host/message_utils.py）：
    {message_id, timestamp(str), platform, session_id,
     message_info: {user_info{user_id,...}, group_info|None, additional_config},
     raw_message: [{type, data, ...}], is_notify, is_command, ...,
     processed_plain_text}
    """
    if not isinstance(message, dict):
        return None
    # 戳一戳、撤回等通知不是聊天消息，跳过
    if message.get("is_notify"):
        return None

    mi = message.get("message_info") or {}
    ui = mi.get("user_info") or {}
    uid = ui.get("user_id")
    gid = (mi.get("group_info") or {}).get("group_id")
    segments = [s for s in (_seg_to_dict(x) for x in (message.get("raw_message") or [])) if s]
    mid = message.get("message_id")

    return {
        "message_id": str(mid) if mid is not None else None,
        "time": _normalize_ts(message.get("timestamp")),
        "stream_id": message.get("session_id"),
        "user_id": str(uid) if uid else None,
        "message": segments,
        "plain_text": str(message.get("processed_plain_text") or ""),
        "is_private": gid is None,
        "raw": message,
    }


def _find_user_id(message: dict, kwargs: dict) -> str | None:
    """从 message_base_info / raw_message / 顶层 kwargs 里挖发送者 user_id。"""
    candidates = [
        message.get("message_base_info"),
        message.get("raw_message") if isinstance(message.get("raw_message"), dict) else None,
        kwargs.get("chat_stream") if isinstance(kwargs.get("chat_stream"), dict) else None,
        kwargs,
    ]
    for src in candidates:
        if not isinstance(src, dict):
            continue
        ui = src.get("user_info")
        if isinstance(ui, dict) and ui.get("user_id"):
            return str(ui["user_id"])
        for key in ("user_id", "sender_id", "uid"):
            if src.get(key):
                return str(src[key])
        sender = src.get("sender")
        if isinstance(sender, dict) and sender.get("user_id"):
            return str(sender["user_id"])
    return None


def extract_message(kwargs: dict) -> dict | None:
    """把 ON_MESSAGE 事件参数归一化成插件内部的消息 dict。

    兼容三种形态：
    1. kwargs["message"] 是 MaiMessages 实例（有 message_segments 属性）
    2. kwargs["message"] 是 RPC dict
    3. 字段直接平铺在 kwargs 上
    """
    msg = kwargs.get("message")

    def _get(obj, key, default=None):
        if isinstance(obj, dict):
            return obj.get(key, default)
        return getattr(obj, key, default)

    segments_raw = _get(msg, "message_segments")
    if segments_raw is None:
        segments_raw = kwargs.get("message_segments") or []
    segments = [s for s in (_seg_to_dict(x) for x in segments_raw) if s]

    mbi = _get(msg, "message_base_info")
    if not isinstance(mbi, dict):
        mbi = kwargs.get("message_base_info") or {}

    raw = _get(msg, "raw_message")
    if isinstance(raw, dict):
        # raw_message 可能就是协议侧原始 dict，整体并入 all 存档
        raw_dump = raw
    else:
        raw_dump = None

    stream_id = _get(msg, "stream_id") or kwargs.get("stream_id") or mbi.get("stream_id")
    is_private = _get(msg, "is_private_message")
    if is_private is None:
        is_private = kwargs.get("is_private_message")

    mid = mbi.get("message_id") or kwargs.get("message_id")
    ts = _normalize_ts(mbi.get("time") or kwargs.get("time"))

    out = {
        "message_id": str(mid) if mid is not None else None,
        "time": ts,
        "stream_id": stream_id,
        "user_id": _find_user_id({"message_base_info": mbi, "raw_message": raw_dump}, kwargs),
        "message": segments,
        "plain_text": _get(msg, "plain_text") or kwargs.get("plain_text") or "",
        "is_private": bool(is_private) if is_private is not None else None,
        "raw": raw_dump,
    }
    return out


def normalize_db_record(record: dict, stream_id: str = "") -> dict | None:
    """把 Host 消息库查询结果转成内部消息 dict（补抓用）。

    Host 的 message_service 返回的对象经 _serialize_messages 序列化后
    与 Hook 载荷同构（message_id / timestamp(str) / message_info /
    raw_message / processed_plain_text），直接复用 normalize_hook_message。
    """
    msg = normalize_hook_message(record)
    if msg is None:
        return None
    if not msg.get("stream_id"):
        msg["stream_id"] = stream_id
    return msg


# ============ 单条消息保存（幂等） ============
async def save_message(ctx, message: dict, depth: int = 0):
    message_id = message.get("message_id")

    # 幂等：已保存过就直接跳过
    if message_id is not None and message_id in storage.get_index():
        return

    for seg in message.get("message", []):
        if not isinstance(seg, dict):
            continue
        seg_type = seg.get("type")
        data = seg.get("data", {})

        if seg_type == "text":
            text = data.get("text", "")
            # 黑名单（保留逻辑，默认关闭）
            if config.get("blacklist_enabled") and any(
                text.startswith(p) for p in (config.get("blacklist_prefixes") or [])
            ):
                continue
            if text.strip():
                if config.get("classify_txt"):
                    # 分类写入 links_/japanese_/numbers_/other_NNN.txt
                    prefix = classifier.get_category(
                        text, config.get("japanese_ratio"))
                else:
                    # 与原部署版一致：全部写 all_NNN.txt
                    prefix = "all"
                storage.append_text(text, prefix)

        elif seg_type == "image":
            url = data.get("url")
            filename = (data.get("file") or data.get("name")
                        or f"{message_id}.jpg")
            if logger:
                logger.info(
                    f"[quest] 处理图片段 mid={message_id} keys={list(data.keys())} "
                    f"b64len={len(data.get('base64') or '')} url={bool(url)}"
                )
            if data.get("base64") or (isinstance(data.get("file"), str)
                                      and len(data.get("file")) > 512):
                # 段里直接带 base64 内容（file 字段过长时通常是 base64）
                payload = data.get("base64") or data.get("file")
                await downloader.save_base64(
                    payload, config.PIC_DIR, filename, message_id, "image")
            elif url:
                await downloader.download_file(
                    url, config.PIC_DIR, filename, message_id, "image")
            else:
                storage.log_error(message_id, "", "image", "no url/base64 in image segment")

        elif seg_type == "record":
            # QQ 原生语音不下载
            pass

        elif seg_type in ("file", "video"):
            url = data.get("url")
            fallback_ext = ".mp4" if seg_type == "video" else ".bin"
            filename = (data.get("file") or data.get("name")
                        or f"{message_id}{fallback_ext}")
            if data.get("base64"):
                await downloader.save_base64(
                    data["base64"], config.FILE_DIR, filename,
                    message_id, seg_type)
            elif url:
                await downloader.download_file(
                    url, config.FILE_DIR, filename, message_id, seg_type)
            else:
                storage.log_error(
                    message_id, "", seg_type, f"no url/base64 in {seg_type} segment")

        # forward / node：MaiBot 插件侧没有 get_forward_msg 通道，不展开，
        # 原始消息已在 write_all 里留档
        # reply / at / face：不做特殊处理，与原版一致

    if message_id is not None:
        storage.add_index(message_id)


# ============ 补抓（Host 消息数据库） ============
async def _fetch_db_page(ctx, chat_id: str, anchor_time: float) -> list[dict]:
    """从 Host 消息库拉一页比 anchor_time 更早的消息，按时间降序返回。

    Host 端 get_recent 实际按 hours 窗口查询（默认 24h），get_by_time_in_chat
    的 end_time 传最旧一条的时间实现向前翻页。记录的时间字段是 timestamp（str）。
    """
    if anchor_time <= 0:
        result = await ctx.message.get_recent(chat_id, config.get("fetch_page_size"))
    else:
        result = await ctx.message.get_by_time_in_chat(
            chat_id, 0, anchor_time - 1)
    if not isinstance(result, list):
        return []
    records = [r for r in result if isinstance(r, dict)]
    records.sort(key=lambda r: _normalize_ts(r.get("timestamp") or r.get("time")), reverse=True)
    return records[: config.get("fetch_page_size")]


async def catch_up(ctx, user_id: str):
    set_catching_up(True)
    if logger:
        logger.info(f"[quest] 开始从 Host 消息库补抓私聊历史，用户 {user_id}")

    hit_boundary = False
    try:
        storage.load_index()  # 防御：确保 index 已加载

        stream = await ctx.chat.get_stream_by_user_id(user_id, platform="qq")
        if not stream:
            if logger:
                logger.info(f"[quest] 用户 {user_id} 还没有私聊流，跳过补抓")
            return
        chat_id = stream.get("stream_id") or stream.get("session_id") or stream.get("chat_id")
        if not chat_id:
            if logger:
                logger.warning(f"[quest] 私聊流缺少 stream_id，跳过补抓: {stream}")
            return

        # 时间窗口：只补最近 N 天，超出窗口的历史不抓
        max_days = config.get("catch_up_max_days")
        cutoff = time.time() - max_days * 86400 if max_days > 0 else 0

        cp = storage.read_checkpoint()
        if cp and cp.get("user_id") == user_id:
            if cutoff and cp.get("last_time", 0) < cutoff:
                if logger:
                    logger.info(f"[quest] 存档点超出 {max_days} 天时间窗口，忽略: {cp}")
                storage.delete_checkpoint()
                anchor = 0.0
            else:
                anchor = cp.get("last_time", 0)
                if logger:
                    logger.info(f"[quest] 从存档点继续: {cp}")
        elif cp:
            if logger:
                logger.warning(f"[quest] 存档点属于其他用户，忽略: {cp.get('user_id')}")
            anchor = 0.0
        else:
            anchor = 0.0

        total = 0
        max_count = config.get("catch_up_max_count")
        prev_anchor = None

        while total < max_count:
            try:
                records = await _fetch_db_page(ctx, chat_id, anchor)
            except Exception as e:
                if logger:
                    logger.error(f"[quest] 消息库翻页失败: {e}")
                break

            if not records:
                # 历史到头
                hit_boundary = True
                break

            messages = [m for m in (normalize_db_record(r, chat_id) for r in records) if m]

            # 只补目标用户本人的消息（DB 记录含双方，bot 自己的也会被返回）
            messages = [m for m in messages if m.get("user_id") == user_id]

            # 时间窗口过滤：超出 N 天的消息不保存；整页都超窗即视为边界
            if cutoff:
                messages = [
                    m for m in messages
                    if not m.get("time") or m.get("time", 0) >= cutoff
                ]
                if not messages:
                    if logger:
                        logger.info(f"[quest] 已到 {max_days} 天时间窗口边界，停止补抓")
                    hit_boundary = True
                    break

            if not messages:
                # 本页全是对方/bot 的消息，锚点无法推进，直接结束
                hit_boundary = True
                break

            oldest = min(messages, key=lambda m: m.get("time", 0))
            new_anchor = oldest.get("time", 0)

            # 边界快照：必须在保存之前计算——保存后整页都会进入 index
            page_all_old = all(
                m.get("message_id") in storage.get_index() for m in messages
            )

            # 只处理不在 index 里的消息（幂等，避免重复写 all/txt/图片）
            for msg in messages:
                if msg.get("message_id") in storage.get_index():
                    continue
                storage.write_all(msg)
                try:
                    await save_message(ctx, msg)
                except Exception as e:
                    if logger:
                        logger.error(f"[quest] 补抓消息处理失败 {msg.get('message_id')}: {e}")

            total += len(messages)

            # 护栏：anchor 不前进说明翻页失效
            if new_anchor == prev_anchor:
                if logger:
                    logger.warning("[quest] anchor 未前进，停止补抓（翻页可能失效）")
                break
            prev_anchor = new_anchor

            if page_all_old:
                hit_boundary = True
                if logger:
                    logger.info("[quest] 整页已缓存，追上边界，停止补抓")
                break

            anchor = new_anchor
            storage.write_checkpoint(
                user_id=user_id,
                message_seq=anchor,
                message_id=oldest.get("message_id", 0),
                time=anchor,
            )

            if total >= max_count:
                if logger:
                    logger.info(f"[quest] 达到最大条数 {max_count}，停止")
                break

            await asyncio.sleep(config.get("fetch_interval_sec"))
    finally:
        # 无论成功/异常，开关必须复位
        set_catching_up(False)
        if logger:
            logger.info("[quest] 补抓结束")

    # 只在真正追上边界/历史到头时删存档点；异常、达上限、翻页失效都保留。
    # 且只删属于当前用户的存档点（多用户轮转场景）。
    if hit_boundary:
        cp_now = storage.read_checkpoint()
        if cp_now is None or cp_now.get("user_id") == user_id:
            storage.delete_checkpoint()
        elif logger:
            logger.info(f"[quest] 存档点属于其他用户 {cp_now.get('user_id')}，保留")
    elif logger:
        logger.info("[quest] 保留存档点，下次启动继续")


async def process_pending(ctx):
    """处理补抓期间实时落盘的新消息"""
    items = storage.read_pending()
    if not items:
        return
    if logger:
        logger.info(f"[quest] 处理 pending: {len(items)} 条")
    failed = []
    for msg in items:
        try:
            await save_message(ctx, msg)
        except Exception as e:
            if logger:
                logger.error(f"[quest] pending 处理失败: {e}")
            failed.append(msg)

    if failed:
        # 失败项原样保留，下次启动重试
        with open(config.PENDING_PATH, "w", encoding="utf-8") as f:
            for msg in failed:
                f.write(json.dumps(msg, ensure_ascii=False) + "\n")
        if logger:
            logger.warning(f"[quest] {len(failed)} 条 pending 处理失败，保留待重试")
    else:
        storage.delete_pending()
        if logger:
            logger.info("[quest] pending 处理完成")
