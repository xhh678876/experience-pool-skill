# experience-pool-skill(创智内网版)

一个 Claude Code / Cursor / Codex / Hermes / OpenClaw 通吃的 **Skill**,把所有 agent 的
会话轨迹自动归档到 sii.edu.cn 内网共享经验池。一行 install,任务结束自动 push,搜索
经验时自动找同类做法。

> 完整中文文档:[docs/UPLOAD_LOGIC_AND_MANUAL.md](docs/UPLOAD_LOGIC_AND_MANUAL.md) ——
> HMAC 签名规则、LiteCard 字段、ACL、批量回填、踩坑、检查清单。

## 内网当前部署

| 用途 | 地址 |
|---|---|
| Gateway / 入口(对外) | `http://10.244.66.195:3080` |
| 门户(`/login` / `/register` / `/me`) | `http://10.244.66.195:3080/login` |
| FastAPI(只内网) | `http://10.244.66.195:8081` |
| OPF 深度脱敏(独立 GPU) | `http://10.245.4.167:8085` |

## 一行装好

正确路径:先在 `/me` 复制 bind 命令,然后:

```bash
curl -sSL http://10.244.66.195:3080/install | \
  EXP_AGENT_NAME='user-xxx' \
  EXP_AGENT_SECRET='<portal-issued-secret>' \
  EXP_BASE_URL='http://10.244.66.195:3080' \
  bash
```

(没账号:`http://10.244.66.195:3080/register` 用 `xxx@sii.edu.cn` 邮箱注册,然后到
`/me` 拿 secret。)

## 这套 skill 由 5 部分组成

1. **Skill 清单**——`SKILL.md`,装到 `~/.claude/skills/experience-pool/SKILL.md`,
   Claude Code 自动加载。同样的契约文件分发到 cursor / codex / hermes / openclaw 各自的位置。
2. **多 agent 上传器**——`scripts/exp_uploader.py`(纯 stdlib)。adapter 覆盖
   Claude Code / Hermes / agents-chat / Cursor / Aider / Codex / Continue.dev /
   Open Interpreter,加一个能识别 OpenAI / Anthropic / LangChain / AutoGen / CrewAI /
   LangSmith 形状的 generic JSON ingester。
3. **Bind + consent**——`scripts/exp_consent.py` 和 `exp bind`。门户发的 HMAC 凭据
   写到 `~/.experience-pool/credentials/`,每台机器一份。
4. **Session extractor**——`session-extractor/`,零依赖一次性回填本机历史
   Claude Code / Codex / Hermes / OpenClaw,**`acl=private` 写死**。
5. **Reward annotator**——`scripts/exp_annotator.py`,Synergy 5 维 per-turn 评分
   (`{-1, 0, +1}` × 5 + confidence + reason)。后端 `claude` CLI / Anthropic API /
   OpenAI-compat 自动回退。

## 架构

```
本地 agent 会话                              ┌─────────────────────────┐
   │ ~/.claude/projects/*.jsonl              │ SessionEnd hook         │
   │ ~/.hermes/sessions/*.json[l]            │   → 任务结束立刻上传    │
   │ ~/agents-chat/messages.db               ├─────────────────────────┤
   │ ~/.continue/sessions/*.json             │ systemd / launchd timer │
   │ ~/.codex/sessions/*                     │   每 120s 兜底          │
   │ …                                       │   exp daemon-tick       │
   ▼                                         └────────────┬────────────┘
adapter 拆 block + 客户端 L0 正则脱敏                       │
   │                                                       ▼
   └── HMAC-SHA256 POST /v1/lite/push ──► http://10.244.66.195:3080
                                                          │
   可选:exp_annotator 切 (user, assistant)               ▼
   + 后续 K turn,提交 5 维 reward 到                FastAPI(8081)
   POST /v1/lite/rewards                              + L1 正则脱敏
                                                      + L2 OPF(可远程,可 defer)
                                                      + SQLite + vectors
                                                      + trajectory sidecar 文件
```

## 自动同步覆盖

装好默认开:

| Source | 位置 | Auto |
|---|---|---|
| Claude Code | `~/.claude/projects/**/*.jsonl` | SessionEnd hook 立刻上传 + daemon 兜底 |
| OpenClaw / OpenClaw-sjtu | 共用 `~/.claude/projects/`(+ `~/.openclaw/`) | 同上 |
| Hermes | `~/.hermes/sessions/*.json[l]` | daemon(2 min) |
| agents-chat | `~/agents-chat/messages.db`(SQLite by `thread_id`) | daemon(2 min) |
| Continue.dev | `~/.continue/sessions/*.json` | daemon(2 min) |
| Codex CLI | `~/.codex/sessions/**` | daemon(2 min) |
| Cursor | `~/Library/.../Cursor/User/**/state.vscdb` | 手动 `exp push` |
| Aider | `<cwd>/.aider.chat.history.md` | 手动 |
| Open Interpreter | `~/Library/.../Open Interpreter/**` | 手动 |
| 任意 JSON | `{messages|trajectory|history|chat_history|runs}` | `exp push-file` |

要改:`EXP_AUTO_SOURCES=claude-code,hermes,...`。

## install 变种

```bash
# 默认(auto-sync 开,acl=private,daemon 120s)
curl -sSL http://10.244.66.195:3080/install | bash

# 指定 agent 名(共享机器推荐)
curl -sSL http://10.244.66.195:3080/install | \
  EXP_AGENT_NAME=alice EXP_TEAM=platform bash

# 关掉所有自动同步,只装 CLI
curl -sSL http://10.244.66.195:3080/install | \
  EXP_SKIP_HOOK=1 EXP_SKIP_DAEMON=1 bash

# 限定 auto-sync 的 source
curl -sSL http://10.244.66.195:3080/install | \
  EXP_AUTO_SOURCES=claude-code,hermes bash

# 改默认 ACL
curl -sSL http://10.244.66.195:3080/install | \
  EXP_AUTO_ACL=team:platform bash

# 装好就批量回填本机历史
curl -sSL http://10.244.66.195:3080/install | \
  EXP_BACKFILL=1 bash
```

## 装好之后

```bash
exp whoami
exp daemon-state                          # 看每个 source 同步到哪
exp daemon-tick --dry-run -v              # 看下次会同步什么
exp list-sessions --source claude-code
exp push-latest --acl team:platform       # 一次性 push 当前 session
exp search --q "FastAPI HMAC 签名失败" --top-k 5
exp push --session <id> --annotate        # 抽取 + 评分 + push
exp get-rewards --experience-id <eid>
exp annotate-existing                     # 用别的模型重新评分
```

## 批量回填(零依赖,session-extractor)

适合一台新机器一次性把过去几周的 Claude Code / Codex 历史全捞上来,不需要预装
`exp` CLI:

```bash
curl -fsSL http://10.244.66.195:3080/session-extractor/run.sh | \
  EXP_AGENT_NAME='user-xxx' \
  EXP_AGENT_SECRET='<portal-issued-secret>' \
  EXP_BASE_URL='http://10.244.66.195:3080' \
  EXP_EXTRACTOR_FLAGS='--max-mb 0 --sleep 0 --verbose' \
  bash
```

`session-extractor` **硬编码 `acl=private`**,服务端按内容指纹去重,**重跑安全**。
门户 `/me` 页有同款一行命令(自动带好 secret)。

## Reward schema(Synergy 兼容)

判分模型每个 `(user, assistant)` 对会看到三段:

```text
<user>        用户请求
<assistant>   助手回复(text + [Tool: name] 调用)
<subsequent>  后续 K 个 user/assistant turn —— 延迟反馈信号
```

输出严格 JSON:

```json
{
  "outcome": 1, "intent": 0, "execution": 1,
  "orchestration": 0, "expression": 1,
  "confidence": 0.7,
  "reason": "user built on the result without correction"
}
```

每维 `{-1, 0, +1}`、`confidence ∈ [0,1]`。存到 `/v1/lite/rewards`,主键
`(experience_id, turn_index, judge_model)`,所以同一条 trace 上可以共存多个 judge。

## 隐私

- **客户端正则**:Anthropic / OpenAI / Stripe / GitHub / AWS key、URL credentials、
  邮箱、IPv4 等,先在本机抹一遍才上传
- **服务端三层防线**:L1 正则(始终)→ L2 OPF 深度脱敏(可远程 / 可 defer)→
  L3 LLM 商业敏感判定(高严重度命中触发)
- 凭据 `~/.experience-pool/credentials/*.json` 是 0600,**不离开本机**
- daemon 默认每条 session ≤ 4 MB,回填可以 `--max-session-kb 32768`

## 文件结构

```
SKILL.md                # Claude Code skill 清单
agent-contract.md       # 行为契约(分发到各 agent runtime)
README.md               # 当前文件
docs/UPLOAD_LOGIC_AND_MANUAL.md   # 完整中文手册
scripts/install.sh      # 一行 install 脚本(也由服务端 /install 提供)
scripts/exp_uploader.py # 多 agent 上传器(stdlib only)
scripts/exp_consent.py  # 本机 consent / pending / revoke 工具
scripts/exp_annotator.py# Synergy 5 维评分注释器
scripts/session_start.sh# Claude Code SessionStart hook(注入 [task-summary] 约定)
session-extractor/      # 独立私有回填工具
LICENSE                 # MIT
```

## Self-host

参考服务:FastAPI + SQLite + Caddy / Next.js gateway,4 GB RAM 够。源码在内网
git 仓库。要把 skill 指到自己的 gateway,装的时候 `EXP_BASE_URL=...` 即可。

## 卸载

```bash
# Linux
systemctl --user disable --now expool-daemon.timer
# macOS
launchctl bootout gui/$(id -u)/com.experience-pool.daemon

rm -rf ~/.experience-pool

# 手动从 ~/.claude/settings.json 里 hooks.SessionEnd / hooks.SessionStart
# 删掉 experience-pool 相关条目(install.sh 是按 needle 匹配清理,re-install 时也会清旧的)
```

## License

MIT —— 见 [LICENSE](LICENSE)。
