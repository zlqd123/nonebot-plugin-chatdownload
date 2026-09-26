import json

from nonebot import on_message
from nonebot.adapters.onebot.v11 import Bot, MessageEvent

from .config import META
from .storage import write_all, append_pending
from . import history
from .history import save_message

matcher = on_message(priority=100, block=False)


@matcher.handle()
async def handle_message(bot: Bot, event: MessageEvent):
    # 只处理私聊
    if event.message_type != "private":
        return

    user_id = str(event.get_user_id())
    if user_id not in META["target_users"]:
        return

    try:
        # pydantic v2 起 .json() 已弃用；save_message 内部会展开合并转发
        raw = json.loads(event.model_dump_json())
    except Exception:
        return

    # all 日志
    write_all(raw)

    if history.is_catching_up():
        # 补抓期间只落盘，等补抓结束后统一处理
        append_pending(raw)
    else:
        await save_message(bot, raw)
