---
name: experience-pool
description: 创智内网共享经验池接入 skill。每次任务结束自动把会话轨迹脱敏后归档到本人 private 库;开始新任务前可先 search 同类问题的关键步骤。支持 Claude Code / Cursor / Codex / Hermes / OpenClaw / agents-chat,以及任意 OpenAI / Anthropic 形状的 messages JSON。HMAC 签名 + per-user ACL,默认 private。
version: 0.5.0
license: MIT
homepage: https://github.com/xhh678876/experience-pool-skill
triggers:
  - 上传经验
  - 共享经验
  - 查找同类做法
  - search experience pool
  - upload trajectory
  - record what worked
  - 这次任务做完了
---

# experience-pool(创智内网版)

把多 agent 的会话轨迹归档到 sii.edu.cn 内网共享经验池。

## 内网当前部署

| 用途 | 地址 |
|---|---|
| Gateway / 入口 | `<EXP_BASE_URL>` |
| 门户(注册 / 登录 / `/me`) | `<EXP_BASE_URL>/login` |
| FastAPI(只内网) | `<EXP_BASE_URL>` |
| OPF 深度脱敏(独立 GPU) | `http://10.245.4.167:8085` |

## 文档导航

- **[`docs/UPLOAD_LOGIC_AND_MANUAL.md`](docs/UPLOAD_LOGIC_AND_MANUAL.md)** —— 上传契约、HMAC 签名、LiteCard 字段、ACL、批量回填、踩坑、检查清单
- **[`docs/SANITIZATION.md`](docs/SANITIZATION.md)** —— 四层脱敏管道(L0 客户端正则 → L1 服务端正则 → L2 OPF 深度 → L3 LLM 判定 + strict-public 发布审查),每条规则抓什么、漏什么、怎么改
- [`agent-contract.md`](agent-contract.md) —— 给 agent 看的行为契约(任务结束自动上传 + 通知用户的格式)

## 第一次用(本机首次接入)

1. 浏览器打开 `<EXP_BASE_URL>/login`,用 `xxx@sii.edu.cn` 邮箱注册 / 登录
2. 进 `/me` 页,复制「绑定本机」面板的 curl 一行
3. 终端粘贴:

```bash
curl -sSL <EXP_BASE_URL>/install | \
  EXP_AGENT_NAME='user-xxx' \
  EXP_AGENT_SECRET='<portal-issued-secret>' \
  EXP_BASE_URL='<EXP_BASE_URL>' \
  bash
```

`install.sh` 是幂等的,重复跑安全。它会做 6 件事:

1. 装 `exp_uploader.py` / `exp_consent.py` / `exp_annotator.py` 到 `~/.experience-pool/bin/`
2. 写凭据到 `~/.experience-pool/credentials/<name>.json`(0600)
3. 检测本机 agent runtime(claude-code / cursor / codex / hermes / openclaw),把
   `agent-contract.md` 分发到对应位置(下面一节列了)
4. 给 Claude Code 写 `SessionEnd` + `SessionStart` hook,旧实验性 hook 会被识别清掉
5. 装 systemd user timer(Linux)或 launchd LaunchAgent(macOS),每 120s 跑一次
   `daemon-tick` 当兜底
6. **不**自动批量回填(怕用户没意识到突然传了一堆)。要回填:
   `bash ~/.experience-pool/run-backfill.sh &`,或在 `/me` 用「批量回填」面板

只想要 CLI、不要 hook 和 daemon:

```bash
EXP_SKIP_HOOK=1 EXP_SKIP_DAEMON=1 bash scripts/install.sh
```

## 三种使用形态

| 目的 | 命令 |
|---|---|
| 看本机有哪些 session | `exp list-sessions --source auto` |
| 手动上传当前最新 session | `exp push-latest --yes` |
| 搜索经验 | `exp search --q "<问题>" --top-k 5` |
| 批量回填本机历史 | `bash ~/.experience-pool/run-backfill.sh &` |
| 一台新机器零依赖回填 | 见 `session-extractor/`(模式 C) |

## 支持的 agent runtime

| Source | 存储位置 | 自动同步 |
|---|---|---|
| Claude Code(+ OpenClaw 分支) | `~/.claude/projects/**/*.jsonl` | SessionEnd hook(任务结束) + daemon 兜底 |
| Hermes | `~/.hermes/sessions/*.json[l]` | daemon(2 min) |
| agents-chat | `~/agents-chat/messages.db`(SQLite by `thread_id`) | daemon(2 min) |
| Continue.dev | `~/.continue/sessions/*.json` | daemon(2 min) |
| Codex CLI | `~/.codex/sessions/**` | daemon(2 min) |
| Cursor | `~/Library/.../Cursor/User/**/state.vscdb` | 手动 `exp push` |
| Aider | `<cwd>/.aider.chat.history.md` | 手动 |
| Open Interpreter | profile 目录 | 手动 |
| 任意 JSON | `{messages|trajectory|history|chat_history|runs}` 形状 | `exp push-file` |

generic adapter 认识 OpenAI / Anthropic / LangChain / AutoGen / CrewAI / LangSmith
的请求 dump 形状。

## 常用命令

```bash
# 状态 + 自检
exp whoami
exp daemon-state                          # 看每个 source 同步到哪
exp list-sessions --source hermes -v
exp search --q "FastAPI HMAC 签名失败" --top-k 5

# 手动 push
exp push --session <id-or-prefix> --acl team:platform
exp push-latest                           # 当前 source 最新的一条
exp push-all --source hermes --since 2026-04-01

# 任意 JSON
exp push-file --file traj.json --task csv_analysis --acl public

# 评分(可选)
exp push --session <id> --annotate --annotate-model claude-haiku-4-5
exp annotate-existing --experience-id <eid> --session <local-id>
exp get-rewards --experience-id <eid>
```

## 隐私(脱敏)

四层防线,详见 [`docs/SANITIZATION.md`](docs/SANITIZATION.md):

| 层 | 什么时候 | 抓什么 |
|---|---|---|
| **L0 客户端正则** | 上传前在本机 | Anthropic / OpenAI / GitHub / AWS / Stripe / Slack key、URL credentials、邮箱、手机号、身份证、IPv4、home path 用户名等 |
| **L1 服务端正则** | 每次 push,始终 | 同 L0(双保险) |
| **L2 OPF 深度脱敏** | 服务端 defer 到 worker(GPU 远程) | 8 类标准 PII —— 散在自由文本里的人名、地址、机构名、SSN、医疗记录等 |
| **L3 LLM 商业敏感判定** | 仅 L1 高严重度命中时 | "这条 trace 是不是涉及商业机密 / 安全敏感",决定是否上人工审 |

外加:

- **strict-public 发布审查** —— 用户点「发布到 community」时跑,**检测就 reject 不替换**:
  `file://` URI、`vscode-resource://`、私有 IP、绝对路径、session UUID 等会指纹
  机器的内容,任何一条命中 publish 就被阻止。
- **凭据 `~/.experience-pool/credentials/*.json`** 是 0600,**不离开本机**。
- **`session-extractor/` 硬编码 `acl=private`**,无法绕过 —— 批量回填只能传到本人 private。
- **每条 session 默认上限 4 MB**(`--max-session-kb 4096`),回填时可放宽。

`tool_calls[].input` 是嵌套结构,客户端 / 服务端都**递归**进每个字符串叶子,
不会被 nesting 蒙混。

## 任务结束自动打标题(零成本)

一个 session 通常包含多个独立子任务。上传时按主题切片,**每段都需要自己的标题**——
session 结束时一个标记只能覆盖最后一段。

**每完成一个子任务,在收尾的助手回复尾部加一行**(不只是 session 结束):

```text
[task-summary]: <动词 + 对象,≤80 char>
```

什么时候打:

- 你刚刚交付完一个有头有尾的子任务(给出答案 / 写完代码 / 修好 bug),用户即将转向下一件事
- 用户的下一条消息切换主题 → 你应该在**前一条**回复就打上
- session 即将结束 → 最后保险

格式规则:

- 动词 + 对象,≤80 char
- 匹配用户语言(中文用户用中文,英文用户用英文)
- 描述**做完了什么**,不要复述用户字面问题
- 单行,无引号,无句末标点
- 纯打招呼 / 澄清没产出的可以跳过

例:

```text
[task-summary]: 排查并修复 Caddy ACME 80 端口防火墙阻塞
[task-summary]: Translate project README from Chinese to English
[task-summary]: 上传机械振动作业到 Canvas
```

每个 marker 被 `_extract_task_summary_title()` 解析成那段的 `intent`。**不耗任何额外推理**。
没打的话,服务端会:

1. 在 push 后**后台跑**一次本地 `claude -p` 总结整段对话(`title_refine` 模块)
2. 还是不行就回退到第一条真实用户消息的第一句

服务端的 title 改写在每次 push 后自动跑,不阻塞响应。

## 输出风格

skill 被调用时,**不要把 `exp ...` 的原始 JSON 贴给用户**。要总结:复用了哪条经验、
为什么匹配、适配了哪几步。让用户感知到「pool 起作用了」,而不是看到一堆调试输出。

## Agent 契约分发位置

`install.sh` 检测到本机 agent runtime 后,把 `agent-contract.md` 写到对应位置:

| Runtime | 写到哪 |
|---|---|
| Claude Code | `~/.claude/skills/experience-pool/SKILL.md`(自动加载) |
| Cursor | `~/.cursor/rules/experience-pool.md`(全局规则) |
| Codex | `~/.codex/AGENTS.md`(append,不覆盖用户自定义) |
| Hermes / OpenClaw | `~/.<runtime>/skills/experience-pool/SKILL.md` + `~/.<runtime>/AGENTS.md` |
| 其它 | `~/.experience-pool/agent-contract-<name>.md`(操作员手动 wire) |

不同 runtime 看到**同一份**契约,行为统一。

## 排错速查

| 现象 | 看哪 |
|---|---|
| `exp whoami` 报 no credential | 没 bind / `EXP_AGENT_NAME` 错了 |
| `401 bad signature` | secret 跟 server 不匹配 / body 字节被 shell 二次转义 |
| 标题成 `<transcript>` 或 conversational 整段 | `docs/UPLOAD_LOGIC_AND_MANUAL.md` §5、§9 |
| 上传量异常翻倍 | LLM 子进程触发 SessionEnd 递归,看 §9 |
| row 永远 `layer1_only`,`strict_redactions` 永远空 | OPF backfill worker 没启动,见 `docs/SANITIZATION.md` §2 |
| 撤回后再传被指纹挡住 | `DELETE FROM content_fingerprints WHERE experience_id=?` |
| `/me` 看不全 | 翻页 `?page=2` |
| Next.js proxy 下 404 | `EXP_UI_PUBLIC_URL` 没设,重 build |

## Self-host

参考实现:FastAPI + SQLite + Caddy / Next.js gateway,4 GB RAM 够。当前内网部署在
主 API + UI pod(对外用 sii vscode notebook proxy 暴露 3080)+ 独立 OPF GPU
机器(`10.245.4.167:8085`)两台。源码在 git 仓库,
见 homepage。
