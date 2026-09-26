from nonebot import get_driver
from . import monitor  # noqa: F401  触发 matcher 注册
from .history import on_bot_connect  # noqa: F401

driver = get_driver()
