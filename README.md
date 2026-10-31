# MaiBot 私聊消息归档插件（chatdownload）

改写自 nonebot-plugin-chatdownload（NoneBot2 + OneBot v11 版），适配 **MaiBot 新版插件 SDK（`maibot_sdk`）**。

持续监听指定用户的私聊消息，按内容分类保存文本、下载图片和文件，同时保留完整的每周 JSONL 日志。

---

## 功能概览（与原版对齐）

- **只处理私聊**：群聊消息全部忽略（`is_private_message` 过滤）
- **只处理指定用户**：`target_users` 配置
- **分类保存**（`classify_txt: true` 时）：
  - 链接 → `txt/links_*.txt`
  - 日文（假名占比 ≥ 30%）→ `txt/japanese_*.txt`
  - 纯数字 → `txt/numbers_*.txt`
  - 其他文本 → `txt/other_*.txt`
  - 默认关闭，统一写 `txt/all_*.txt`（与原部署版行为一致）
- **图片/文件下载**：消息段带 URL 用 httpx 下载；带 base64 直接落盘
- **完整日志**：按消息自身时间戳分周写入 `all/YYYY-Www.jsonl`
- **历史补抓**：从 MaiBot Host 消息数据库向前翻页，直到追上缓存边界
- **断点续传**：补抓中断保留存档点 `cache/checkpoint.json`，下次继续
- **不丢新消息**：补抓期间实时消息落盘 `cache/pending.jsonl`，结束后统一处理
- **幂等保存**：`cache/index.json` 记录已保存 message_id，重复不写
- **失败记录**：下载失败写入 `error.jsonl`，不重试

## 与原版的关键差异

| 项目 | 原版（NoneBot + OneBot v11） | 本版（MaiBot SDK） |
|------|------------------------------|--------------------|
| 运行位置 | NoneBot 进程内插件 | 独立 Runner 子进程，经 RPC 访问 Host |
| 消息来源 | OneBot v11 事件 | `@EventHandler(ON_MESSAGE)` |
| 历史补抓 | 协议端 API `get_friend_msg_history` 向前翻页 | Host 消息数据库（`ctx.message.get_recent` / `get_by_time_in_chat`），锚点为最旧消息时间 |
| 合并转发 | `get_forward_msg` 展开最多 2 层 | **不展开**（无协议端通道），原始消息记入 all 日志 |
| 数据目录 | 硬编码 `/app/quest` | Host 授予的插件数据目录（`ctx.paths.data_dir`） |
| 配置 | `meta.yaml` | `config.toml`（支持 WebUI Schema 与热更新） |
| bot 自身消息 | `self_id` 过滤 | `target_users` 白名单本身即排除（不建议把 bot 自己的号加进去） |
| 图片 URL 时效 | QQ 图床 rkey 过期，尽快下载 | 消息段若只有 base64 则直接落盘，无此问题 |

## 安装

1. 把 `maibot_plugin_chatdownload/` 整个目录复制到 MaiBot 的 `plugins/` 下：
   ```
   MaiBot/
   └── plugins/
       └── maibot_plugin_chatdownload/
           ├── _manifest.json
           ├── plugin.py
           ├── config.toml
           ├── requirements.txt
           ├── quest_core/
           │   ├── __init__.py
           │   ├── classifier.py
           │   ├── config.py
           │   ├── downloader.py
           │   ├── history.py
           │   └── storage.py
           └── ...
   ```
2. 安装依赖：`pip install httpx`
3. 编辑 `config.toml`，把 `target_users` 改成要归档的 QQ 号
4. 启动 MaiBot，插件自动加载

> `_manifest.json` 的字段若与你的 Host 版本要求不一致，按 Host 报错提示补齐即可（本插件不使用 LLM Provider / 特殊 capabilities）。

## 数据目录

数据保存在 Host 分配的插件持久化目录（`ctx.paths.data_dir`，通常映射到 `data/plugins/<插件名>/`）：

```
<data_dir>/
├── txt/          # 分类文本 all_001.txt ...
├── pic/          # 图片
├── file/         # 文件、视频
├── all/          # 每周完整日志 2026-W44.jsonl
├── cache/
│   ├── checkpoint.json   # 补抓存档点
│   ├── pending.jsonl     # 补抓期间实时消息
│   └── index.json        # 已保存 message_id 索引
└── error.jsonl   # 下载失败记录
```

## 配置项

| 配置项 | 默认 | 说明 |
|--------|------|------|
| `target_users` | `[]` | 要归档的 QQ 号列表 |
| `txt_max_lines` | 100 | txt 文件滚动行数阈值 |
| `save_all` | true | 是否写每周 all 日志 |
| `catch_up_max_count` | 200 | 单次补抓最大条数 |
| `catch_up_max_days` | 14 | 补抓时间窗口（天），0 不限制 |
| `startup_wait_sec` | 60 | 启动等待秒数 |
| `fetch_interval_sec` | 30 | 每页拉取间隔 |
| `fetch_page_size` | 20 | 每页条数 |
| `japanese_ratio` | 0.3 | 日文判定阈值 |
| `download_concurrency` | 1 | 下载并发 |
| `classify_txt` | false | 文本按分类分文件 |
| `blacklist_enabled` | false | 黑名单前缀开关 |
| `blacklist_prefixes` | [] | 黑名单前缀列表 |

## 运行流程

与原版一致：

1. **首次启动**（txt/pic/file/index 全空）→ 不补抓，直接实时监听
2. **有数据启动** → 等待 `startup_wait_sec` → 从 Host 消息库按时间向前翻页补抓，直到整页都已缓存（追上边界）
3. **补抓中收到新消息** → 写 all + 落盘 `pending.jsonl`，补抓结束后统一走 `save_message`
4. **补抓中断** → 存档点保留，下次启动从存档点的时间锚点继续
5. 只有追上边界 / 历史到头才删存档点；达上限、异常、翻页失效都保留

## 已知限制

- 合并转发不展开（MaiBot 插件侧无法调用协议端 `get_forward_msg`），只记入 all 日志
- 补抓数据来自 MaiBot 自身数据库：文本可靠；图片/文件段若数据库未保留 URL/base64 则无法补下载
- QQ 原生语音（record 段）不下载，仅记入 all
- 图片下载不重试，失败记录到 `error.jsonl`
- 补抓串行执行，多用户时启动较慢

## 兼容性说明

- 消息解析对 `MaiMessages` 实例 / RPC dict / 平铺 kwargs 三种形态都做了兼容；user_id 依次从 `message_base_info.user_info`、`raw_message`、`chat_stream`、顶层 kwargs 挖掘，挖不到时用 `ctx.chat.get_private_streams()` 反查并缓存
- 如果 Host 事件参数命名有出入，看日志里的 `[quest] 消息解析失败` 或私聊流反查告警，对应调整 `quest_core/history.py` 的 `extract_message`

## 更新日志

### v0.2.0
- 适配 MaiBot 新版 SDK（MaiBotPlugin / EventHandler / ctx 能力代理）
- 补抓通道从 OneBot API 改为 Host 消息数据库
- 配置从 meta.yaml 迁移到 config.toml（config_model + WebUI Schema + 热更新）
- 新增 base64 段落直接落盘、可选文本分类分文件

### 原版 v0.1.0（nonebot-plugin-chatdownload）
- 私聊消息分类归档、历史补抓 + 断点续传、合并转发展开、每周 all 日志、下载失败记录
