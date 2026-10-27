# CLAUDE.md

给在本仓库工作的 AI 助手的说明。人类开发流程见 `CONTRIBUTING.md`，配置项说明见 `CONFIG.md`。

## 硬性规则

1. **提交与 PR 不得署名 Claude**。提交信息、PR 标题和正文都不要出现 `Co-Authored-By: Claude`、
   `Generated with Claude Code` 或任何 AI 署名/致谢行。只写"做了什么"。
2. **不要提交个人数据**：真实 QQ 号、昵称、API key、token、个人绝对路径、私人聊天记录、
   个人项目名。`config.json`、`data/`、`*.db`、`*.log`、`persona-*.md` 已在 `.gitignore` 中，
   不要用 `git add -f` 绕过。
3. **不要破坏跨平台**：新代码要能在 Windows / macOS / Linux 上跑；平台专有调用必须可降级，
   缺依赖时跳过并给出安装指引，而不是直接崩。
4. **不要为了通过测试而放宽断言**。测试表达的是行为契约；改行为要同步改测试并在提交信息里说明。

## 常用命令

```bash
python -m pytest                 # 全量测试（pyproject 已配 testpaths/pythonpath）
python -m pytest tests/test_web_panel.py -q
ruff check .                     # 静态检查（line-length 100，已忽略 E501）
python tools/gen_config_doc.py   # 改了配置项后重新生成 CONFIG.md
./start.sh                       # 启动机器人
./start-all.sh                   # 连同 NapCat 一起启动（本机辅助脚本）
./stop-all.sh                    # 停止（--force 强制）
```

Python 3.10+（ruff 目标 3.13）。依赖分四份：`requirements.txt`、`-agno`、`-web`、`-voice`。

## 架构地图

入口 `bridge.py:main()`：连接 OneBot WebSocket，收事件 → `handlers.py` 处理 → 模型 → 发回。

| 模块 | 职责 |
| --- | --- |
| `bridge.py` | 事件循环、消息聚合缓冲、主动消息、Web 面板启动、日志轮转配置 |
| `handlers.py` | **主流程**：命令拦截、闲聊/任务判定、媒体描述、提示词组装、回复发送 |
| `message_utils.py` | 消息解析（私聊/群聊）、任务分类、`build_persona_prompt`、回复切分 |
| `agent_client.py` | agno/OpenAI 兼容模型封装、工具装配（`_build_tools`）、会话与长期记忆 |
| `call_policy.py` | 按发送者/群决定权限（`full_access`）并路由到对应 agent |
| `targets.py` | 会话键：私聊 = QQ 号，群聊 = `g:<group_id>` |
| `media.py` | 发送图片/表情/语音/文件，按场景分发 `send_group_msg` / `send_private_msg` |
| `state_db.py` | SQLite：提醒、待办、任务、记忆。变更方法支持可选 `user_id` 做作用域约束 |
| `task_manager.py` | 后台任务队列（单 worker）、阶段汇报、取消语义 |
| `sessions.py` | 线程与会话键映射（`sessions.json`） |
| `config.py` | `DEFAULTS`、`LEGACY_KEYS`、环境变量覆盖、路径解析、启动期校验 |
| `web_panel.py` | FastAPI 工作台：状态、聊天、日志增量读取、提醒/待办/任务管理 |
| `web_panel/static/index.html` | 单文件零构建前端（无 CDN、离线可用） |

其余为按需加载的能力模块：`vision_client` / `video_utils` / `tts_client` / `rag` /
`cn_search` / `astrbot_plugins` / `calendar_todos` / `timetable` / `channels`。

## 改代码时的约定

- **会话键**一律通过 `targets.py`，不要手写 `f"g:{gid}"` 或裸 QQ 号。
- **群聊与私聊共用流程**，差异集中在 `bridge.py` 的事件解析、`handlers.py` 的
  `process_group_buffer`、`media.py` 的场景分发。加功能时两条路径都要考虑。
- **权限**由 `call_policy.run_with_policy(..., full_access=...)` 决定；受限发送者拿不到
  Shell/File 工具。不要让新的执行路径绕过它。
- **模型输出**是人设正文，`sanitize_persona_reply` / `split_persona_reply` 负责去掉
  内部独白并按配置切成一条或多条。`persona_chat_max_segments=1` 时必须整段合并，
  不能只取第一段。
- **提示词**里的功能规则（媒体标记、文件发送、无图提醒）只应描述"怎么发"，
  不要用通用的助手克制话术覆盖用户设定的人格。
- **日志**用 `logging`，不要 `print`；异常要带上下文，但不要打印密钥。

## 加配置项的固定流程

1. 在 `config.py` 的 `DEFAULTS` 加键和默认值；路径类键归入 `_DATA_PATH_KEYS` /
   `_PROJECT_PATH_KEYS` / `_EXTERNAL_PATH_KEYS`。
2. 在 `tools/gen_config_doc.py` 的 `GROUPS` 归组、`DESCRIPTIONS` 写说明。
3. 运行 `python tools/gen_config_doc.py` 重新生成 `CONFIG.md`（缺说明会硬失败）。
4. 兼容旧键名时在 `config.py` 的 `LEGACY_KEYS` 加映射。
5. `config.example.json` 只放必填/最常用键。
6. 补 `tests/test_config.py`，并确认 `tests/test_example_config.py` 仍通过。

## 测试

- `tests/conftest.py` 提供通用夹具；`tests/fake_agent.py` 是模型替身。
- 改 `agent_client.run()` 或 `call_policy.run_with_policy()` 的签名时，**必须同步更新所有
  测试替身**（`fake_run`、`RecordingCodex`、各测试内的 `run()` 定义），否则会大面积 TypeError。
- 部分测试依赖本机环境（silk 编解码、trending 网络代理、视频 libx264），失败时先确认
  是否属于环境缺失，不要直接删测试。

## 本机运行注意

- **NapCat AppImage 必须在它自己的目录里启动**：`AppRun` 把 `$PWD` 当配置目录，换了目录
  就等于空配置，表现为 6099 能开但 OneBot 的 3001 永不监听。`start-all.sh` 已处理。
- `config.json` 是本地文件，其中的 `full_access_qq`、`web_token`、`api_key` 不得进提交。
- Web 面板的 `web_local_no_token` 只对回环地址生效，且默认关闭。
