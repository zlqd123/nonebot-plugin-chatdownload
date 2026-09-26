# nonebot-plugin-chatdownload

一个基于 NoneBot2 + OneBot v11 的**私聊消息归档插件**。持续监听指定好友的私聊消息，按内容分类保存文本、下载图片和文件，同时保留完整的每周日志。Bot 重启后能自动补抓离线期间的历史消息，保证不丢消息。

---

## 功能概览

- **只处理私聊**：群聊消息全部忽略
- **只处理指定用户**：通过 `target_users` 配置
- **分类保存**：
  - 图片 → `pic/`
  - 文件/视频 → `file/`
  - 链接 → `txt/links_*.txt`
  - 日文 → `txt/japanese_*.txt`
  - 纯数字 → `txt/numbers_*.txt`
  - 其他文本 → `txt/other_*.txt`
- **完整日志**：所有消息按自然周写入 `all/YYYY-Www.jsonl`
- **历史补抓**：Bot 重启后自动向前翻页，直到追上缓存边界
- **断点续传**：补抓中途异常保留存档点，下次继续
- **不丢新消息**：补抓期间实时消息落盘 `pending.jsonl`，补抓结束后统一处理
- **幂等保存**：同一 `message_id` 只保存一次，避免重复写入
- **合并转发展开**：最多展开 2 层（可配置）
- **失败记录**：图片/文件下载失败写入 `error.jsonl`

---

## 目录结构

```
/app/quest/
├── txt/                          # 分类文本
│   ├── links_001.txt
│   ├── numbers_001.txt
│   ├── japanese_001.txt
│   └── other_001.txt
├── pic/                          # 图片
│   └── {qq文件名}
├── file/                         # 文件、视频
│   └── {qq文件名}
├── all/                          # 每周完整日志（JSONL）
│   └── 2026-W39.jsonl
├── cache/
│   ├── checkpoint.json           # 补抓存档点
│   ├── pending.jsonl             # 补抓期间实时消息
│   └── index.json                # 已保存 message_id 索引
├── meta.yaml                     # 配置
└── error.jsonl                   # 下载失败记录
```

---

## 安装

### 依赖

- Python 3.10+
- NoneBot2（建议 2.4.0+）
- nonebot-adapter-onebot（v11）
- httpx
- pyyaml

```bash
pip install nonebot2 nonebot-adapter-onebot httpx pyyaml
```

### 放置插件

把 `nonebot_plugin_chatdownload/` 目录放到你的 NoneBot 项目的插件目录下，然后在 `pyproject.toml` 中启用：

```toml
[tool.nonebot]
plugins = ["nonebot_plugin_chatdownload"]
```

如果插件在自定义目录：

```toml
[tool.nonebot]
plugin_dirs = ["awesome_bot/plugins"]
plugins = ["nonebot_plugin_chatdownload"]
```

### 协议端要求

需要支持以下 OneBot v11 扩展 API：

- `get_friend_msg_history` — 获取好友历史消息
- `get_forward_msg` — 展开合并转发

已在 **NapCat** 上验证通过。其他协议端（LLOneBot、Lagrange 等）如果 API 参数名不同，需要自行核对。

---

## 配置

首次启动时，插件会自动把内置的 `meta.yaml` 模板拷贝到 `/app/quest/meta.yaml`。之后所有配置从该文件读取。

```yaml
# 关注用户列表（QQ 号，字符串）
target_users:
  - "302771504"

# 黑名单前缀（代码中已保留逻辑，默认关闭）
# 启用后，以这些前缀开头的消息会被跳过
blacklist:
  enabled: false
  prefixes:
    - "管理后台"
    - "后台管理"
    - "喜报"
    - "悲报"

# 单个 txt 文件大小上限（KB），超过后自动滚动到下一个序号
txt_max_size_kb: 500

# 合并转发最大展开层数
forward_max_depth: 2

# all 日志开关
save_all: true

# 补抓最大条数（防止首次运行拉太多）
catch_up_max_count: 200

# 启动后等待秒数（等协议端稳定）
startup_wait_sec: 60

# 每页拉取间隔秒数（控制 API 频率）
fetch_interval_sec: 30

# 每页拉取条数
fetch_page_size: 20

# 日文占比阈值（0.3 = 30%）
japanese_ratio: 0.3

# 图片/文件下载并发数
download_concurrency: 1
```

### 配置项说明

| 配置项 | 说明 | 建议值 |
|--------|------|--------|
| `target_users` | 要归档的 QQ 号列表 | 按需 |
| `blacklist.enabled` | 是否启用黑名单 | `false` |
| `txt_max_size_kb` | 文本文件滚动阈值 | 500 |
| `forward_max_depth` | 合并转发展开层数 | 2 |
| `save_all` | 是否写 all 日志 | `true` |
| `catch_up_max_count` | 单次补抓最大条数 | 200 |
| `startup_wait_sec` | 启动等待时间 | 60 |
| `fetch_interval_sec` | 翻页间隔 | 30 |
| `fetch_page_size` | 每页条数 | 20 |
| `japanese_ratio` | 日文判定阈值 | 0.3 |
| `download_concurrency` | 下载并发 | 1 |

---

## 运行流程

### 首次启动

1. 创建 `/app/quest/` 及子目录
2. 从插件目录拷贝 `meta.yaml` 到 `/app/quest/`
3. `load_index()`：`index.json` 不存在 → 空集合
4. 等待 60 秒
5. 检查 `txt/`、`pic/`、`file/`、`index.json` 是否为空
6. 全空 → **不补抓**，直接进入实时监听
7. 等待第一条消息到达

### 后续启动（有数据）

1. 读 `index.json`
2. 等待 60 秒
3. 检测到有数据 → 触发补抓
4. 从最新消息向前翻页，直到某页**整页都是已缓存消息**
5. 删除存档点
6. 处理 `pending.jsonl`（补抓期间落盘的实时消息）
7. 进入实时监听

### 补抓期间收到新消息

1. 消息写 `all/`
2. 消息追加到 `pending.jsonl`
3. 补抓结束后统一走 `save_message`

### 补抓中途异常

- 存档点保留
- `pending.jsonl` 保留
- 下次启动从存档点继续

---

## 分类规则

### 优先级（从高到低）

1. **图片** → `pic/`
2. **文件/视频** → `file/`
3. **语音** → 仅记录到 all，不下载
4. **链接** → `txt/links_*.txt`
5. **日文**（假名占比 ≥ 30%）→ `txt/japanese_*.txt`
6. **纯数字**（至少含一个数字，不含字母/汉字/假名）→ `txt/numbers_*.txt`
7. **其他** → `txt/other_*.txt`

### 链接判定

正则匹配以下任一：

- `http://`、`https://`、`ftp://`、`mqqapi://`
- `xxx.xxx` 格式的裸域名（如 `baidu.com`）

所有链接（含 IP）写入同一个 `links_*.txt`，不做域名分文件。

### 纯数字判定

- 至少包含一个数字
- 不含任何字母、汉字、日文假名
- 允许符号、空格、换行

例如：`127。0。0。1:58080` 算数字（中文句号是符号）。

### 日文判定

平假名/片假名（`\u3040-\u30ff`）占比 ≥ 30%。

---

## 消息段处理

| 消息段类型 | 处理方式 |
|------------|----------|
| `text` | 按分类规则写入 txt |
| `image` | 下载到 `pic/`，QQ 文件名，重复加序号 |
| `record` | 仅记录到 all，不下载 |
| `file` | 下载到 `file/` |
| `video` | 下载到 `file/` |
| `forward` | 展开，最多 2 层，子消息也进分类 |
| `reply` | 不处理引用原文 |
| `at` | 视为普通文本 |
| `face` | 仅记录到 all |

---

## 去重机制

- `cache/index.json` 保存所有已保存的 `message_id`
- **历史抓取**：查到已存在则停止
- **实时监听**：不查 index，但写入 index
- **`save_message` 开头幂等判断**：已存在则直接返回，避免重复写入

---

## 存档点机制

### `cache/checkpoint.json`

```json
{
  "user_id": "302771504",
  "last_message_seq": 586151889,
  "last_message_id": 586151889,
  "last_time": 1787377142,
  "updated_at": "2026-09-25T20:10:05+08:00"
}
```

- 每翻一页写入一次
- 只在**追上缓存边界**或**历史到头**时删除
- 异常中断时保留，下次继续
- 带 `user_id`，多用户场景不会串用

### `cache/pending.jsonl`

补抓期间实时消息的落盘文件，每行一条完整消息。补抓结束后逐条处理，成功则删除，失败则保留待重试。

---

## 合并转发处理

- 展开最多 `forward_max_depth` 层（默认 2）
- 兼容 `content` 和 `message` 两种节点字段
- 展开的子消息也会：
  - 写入 `all/`
  - 走 `save_message` 分类保存

---

## 错误记录

`error.jsonl` 每行一条 JSON，记录下载失败：

```json
{"time": "2026-09-25T20:10:05+08:00", "message_id": 123456, "url": "https://...", "type": "image", "error": "HTTP 403", "reason": ""}
```

字段：

- `time`：ISO 8601，精确到秒（+08:00 时区）
- `message_id`：消息 ID
- `url`：下载地址
- `type`：`image` / `file` / `video`
- `error`：异常信息
- `reason`：补充说明（可选）

**不重试**，仅记录。

---

## 常见问题

### Q：为什么首次启动不抓历史？

首次启动时 `txt/`、`pic/`、`file/`、`index.json` 都为空，说明是全新环境，没有可补的边界。直接进入实时监听即可。

### Q：Bot 重启后会重复下载图片吗？

不会。`save_message` 开头会检查 `message_id` 是否已在 `index.json` 中，已存在则直接返回。

### Q：补抓期间收到新消息会丢吗？

不会。实时消息落盘 `pending.jsonl`，补抓结束后统一处理。即使补抓中途崩溃，`pending.jsonl` 也保留。

### Q：存档点什么时候删除？

只在**整页消息都已缓存**（追上边界）或**历史到头**时删除。异常中断、达到最大条数上限时保留。

### Q：为什么图片下载会失败？

QQ 图床的 URL 带 `rkey` 临时授权，可能几小时就过期。插件收到历史消息后会尽快下载。如果失败，记录到 `error.jsonl`，不会重试。

### Q：可以只抓某个用户的部分消息吗？

可以用黑名单。在 `meta.yaml` 中设置 `blacklist.enabled: true` 并配置前缀。代码中调用点已保留，取消注释即可。

### Q：`reverse_order` 参数在别的协议端无效怎么办？

本插件在 NapCat 上验证通过。如果换协议端后翻页失效（同一页反复抓取），优先检查参数名是否为 `reverseOrder`（camelCase），以及 `message_seq` 是否需要 `str` 类型。

### Q：txt 文件超过 500KB 会怎样？

自动滚动到下一个序号：`links_001.txt` → `links_002.txt`。排序按数字后缀，不会出现 `_1000` 排在 `_999` 前面的问题。

### Q：all 日志按什么分周？

按**消息自身的时间戳**分周，不是处理时刻。补抓历史消息时会写入其发生的那一周，保证时间线正确。

### Q：`meta.yaml` 可以直接编辑吗？

可以。插件每次启动都会读取 `/app/quest/meta.yaml`。支持 YAML 注释，改动后重启生效。

### Q：支持多个关注用户吗？

支持，`target_users` 是列表。存档点带 `user_id`，多用户不会串用。但补抓是串行执行的，用户多时启动时间会变长。

---

## 项目结构

```
nonebot_plugin_chatdownload/
├── __init__.py          # 插件入口
├── config.py            # 配置加载、路径定义
├── storage.py           # index、checkpoint、pending、all、txt 读写
├── classifier.py        # 文本分类
├── downloader.py        # 文件下载
├── history.py           # 历史补抓、消息保存、合并转发展开
├── monitor.py           # 实时监听
└── meta.yaml            # 配置模板
```

### 模块依赖

```
monitor → history → storage
                  → classifier
                  → downloader
config ← 所有模块
```

无循环导入。

---

## 已知限制

- 只支持 OneBot v11 协议
- 只在 NapCat 上验证，其他协议端需自行核对 API 参数
- QQ 原生语音不下载（仅记录到 all）
- 合并转发最多展开 2 层（可配置）
- 图片下载不重试
- 补抓是串行的，多用户时启动较慢

---

## 数据安全

- 所有数据保存在 `/app/quest/` 本地目录
- 不涉及任何外部上传
- 文件名经过消毒，防止路径穿越
- `index.json` 只存 `message_id`，不存消息内容

---

## 更新日志

### v0.1.0

- 初始版本
- 私聊消息分类归档
- 历史补抓 + 断点续传
- 合并转发展开
- 每周 all 日志
- 下载失败记录
</｜｜DSML｜｜ parameter>
</｜｜DSML｜｜ invoke>
</｜｜DSML｜｜ calls>