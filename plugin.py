"""nonebot-plugin-chatdownload 的 MaiBot 新版 SDK 移植版。

持续监听指定用户的私聊消息，按内容分类保存文本、下载图片和文件，
同时保留完整的每周日志。重启后从 Host 消息数据库补抓离线期间的历史。

MaiBot 新版插件系统约束：
- 入口文件必须是 plugin.py，定义 create_plugin() 返回 MaiBotPlugin 实例
- 插件运行在独立 Runner 子进程，所有能力通过 self.ctx 获取
- quest_core 包通过 sys.path 注入，包名带前缀避免与其他插件冲突
"""

import asyncio
import sys
from pathlib import Path
from typing import Any

from maibot_sdk import Field, HookHandler, MaiBotPlugin, PluginConfigBase
from maibot_sdk.types import ErrorPolicy, HookMode

# 让 quest_core 可以作为普通包导入（plugin.py 由 Runner 按路径加载，
# 其所在目录不一定在 sys.path 上）
_PLUGIN_DIR = Path(__file__).resolve().parent
if str(_PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR))

from quest_core import config as core_config  # noqa: E402
from quest_core import downloader, history, storage  # noqa: E402


# ============ 配置模型 ============
class PluginSection(PluginConfigBase):
    """[plugin] 节 —— Runner 必需：enabled + config_version。"""

    __ui_label__ = "插件"

    enabled: bool = Field(default=True, description="是否启用插件")
    config_version: str = Field(default="0.2.0", description="配置版本")


class ChatDownloadConfig(PluginConfigBase):
    """私聊消息归档配置。"""

    plugin: PluginSection = Field(default_factory=PluginSection)


    __ui_label__ = "私聊归档设置"

    target_users: list[str] = Field(
        default_factory=list,
        description="要归档的用户 QQ 号列表（字符串）",
        json_schema_extra={"label": "关注用户", "placeholder": "如 [\"123456\"]"},
    )
    txt_max_lines: int = Field(
        default=100,
        description="单个 txt 文件最大行数，超过后滚动到下一个序号；<= 0 表示不滚动、全部写一个 txt",
    )
    save_all: bool = Field(
        default=True,
        description="是否写每周完整日志（all/YYYY-Www.jsonl）",
    )
    catch_up_max_count: int = Field(
        default=200,
        description="单次补抓最大条数（防止首次运行拉太多）",
    )
    catch_up_max_days: int = Field(
        default=14,
        description="补抓时间窗口（天）：只补最近 N 天的历史，0 = 不限制",
    )
    startup_wait_sec: int = Field(
        default=60,
        description="启动后等待秒数（等 Host 与适配器稳定）",
    )
    fetch_interval_sec: int = Field(
        default=30,
        description="每页拉取间隔秒数（控制数据库查询频率）",
    )
    fetch_page_size: int = Field(
        default=20,
        description="每页拉取条数",
    )
    japanese_ratio: float = Field(
        default=0.3,
        description="日文判定阈值（0.3 = 假名占比 30%）",
    )
    download_concurrency: int = Field(
        default=1,
        description="图片/文件下载并发数",
    )
    classify_txt: bool = Field(
        default=False,
        description="文本是否按分类分文件（links_/japanese_/numbers_/other）；关闭则统一写 all_NNN.txt",
    )
    save_dir: str = Field(
        default="",
        description="归档保存根目录；留空用 Host 分配的插件数据目录",
    )
    blacklist_enabled: bool = Field(
        default=False,
        description="是否启用黑名单前缀过滤",
    )
    blacklist_prefixes: list[str] = Field(
        default_factory=list,
        description="黑名单前缀列表（启用后以这些前缀开头的文本跳过）",
    )


def _asdict(model: Any) -> dict:
    if isinstance(model, dict):
        return model
    return model.model_dump()


class ChatDownloadPlugin(MaiBotPlugin):
    config_model = ChatDownloadConfig

    def __init__(self):
        super().__init__()
        self._startup_task: asyncio.Task | None = None

    # ============ 生命周期 ============
    async def on_load(self) -> None:
        core_config.set_config(_asdict(self.config))
        core_config.set_base_dir(
            self.ctx.paths.data_dir,
            override_dir=core_config.get("save_dir"),
        )
        history.set_logger(self.ctx.logger)

        storage.load_index()
        self.ctx.logger.info(
            f"[quest] 插件已加载，数据目录: {core_config.BASE_DIR}，"
            f"关注用户: {core_config.get('target_users')}"
        )

        # 补抓不能阻塞 on_load（会延后 Runner 的 ready 信号），丢到后台任务
        self._startup_task = asyncio.create_task(self._startup_flow())

    async def on_unload(self) -> None:
        if self._startup_task:
            self._startup_task.cancel()
            self._startup_task = None
        history.set_catching_up(False)
        self.ctx.logger.info("[quest] 插件已卸载")

    async def on_config_update(self, scope: str, config_data: dict, version: str) -> None:
        if scope == "self":
            core_config.set_config(config_data)
            self.ctx.logger.info(f"[quest] 配置已更新: version={version}")

    # ============ 启动补抓流程 ============
    async def _startup_flow(self):
        try:
            await asyncio.sleep(core_config.get("startup_wait_sec"))

            # 防御：目录可能在等待期间被删除/移动，重新确保存在
            core_config.set_base_dir(
                self.ctx.paths.data_dir,
                override_dir=core_config.get("save_dir"),
            )

            has_data = (
                any(core_config.TXT_DIR.iterdir())
                or any(core_config.PIC_DIR.iterdir())
                or any(core_config.FILE_DIR.iterdir())
                or core_config.INDEX_PATH.exists()
            )
            has_checkpoint = core_config.CHECKPOINT_PATH.exists()

            if has_data or has_checkpoint:
                for user_id in core_config.get("target_users"):
                    try:
                        await history.catch_up(self.ctx, str(user_id))
                    except Exception as e:
                        # 单个用户失败不阻断其余用户与 pending 处理
                        self.ctx.logger.error(f"[quest] 补抓异常 {user_id}: {e}")
            else:
                self.ctx.logger.info("[quest] 首次启动（无历史数据），不补抓，直接实时监听")

            # 先补抓，再处理 pending
            await history.process_pending(self.ctx)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            self.ctx.logger.error(f"[quest] 启动流程异常: {e}", exc_info=True)

    # ============ 实时监听 ============
    # 注意：本 Host 版本（1.3.x）没有 emit EventType.ON_MESSAGE（bot.py 中已注释），
    # EventHandler(ON_MESSAGE) 永远不会触发；改用消息链路上真实存在的
    # chat.receive.after_process Hook（bot.py 每条入站消息都会调用）。
    @HookHandler(
        "chat.receive.after_process",
        name="chat_download",
        mode=HookMode.OBSERVE,
        error_policy=ErrorPolicy.SKIP,
    )
    async def on_message_hook(self, message=None, **kwargs):
        try:
            msg = history.normalize_hook_message(message)
        except Exception as e:
            self.ctx.logger.warning(f"[quest] 消息解析失败: {e}")
            return {"action": "continue"}
        if not msg:
            return {"action": "continue"}

        # 只处理私聊
        if msg.get("is_private") is False:
            return {"action": "continue"}

        user_id = msg.get("user_id")
        if not user_id:
            return {"action": "continue"}
        if user_id not in [str(u) for u in core_config.get("target_users")]:
            return {"action": "continue"}

        # all 日志（载荷完整留档）
        storage.write_all(msg)

        if history.is_catching_up():
            # 补抓期间只落盘，等补抓结束后统一处理
            storage.append_pending(msg)
        else:
            await history.save_message(self.ctx, msg)

        return {"action": "continue"}


def create_plugin() -> ChatDownloadPlugin:
    return ChatDownloadPlugin()
