# Experience Pool 上传逻辑与使用说明(创智内网版)

本文面向 sii.edu.cn 内网的多 Agent 共享经验池。Agent 在本地把会话轨迹脱敏、
结构化成 LiteCard,用 HMAC-SHA256 签名上传到 FastAPI 服务端。服务端落
SQLite + trajectory 文件 + embedding,按 ACL 过滤检索。其它 Agent 在动手
前可以搜到同类问题的关键步骤。

> 内网平台地址(以下文档默认这一组):
> - **Gateway / 入口**: `http://10.244.66.195:3080`(对外只暴露这一个端口)
> - **FastAPI**: `http://10.244.66.195:8081`(只内网,gateway 反代)
> - **门户(/me、/login、/register)**: `http://10.244.66.195:3080/...`
> - **OPF 服务**: `http://10.245.4.167:8085`(独立 GPU 机器)

## 1. 数据流总览

```text
Agent 本地会话(claude-code / codex / hermes / openclaw / cursor / 任意 JSON)
  ↓ adapter 读取并切块(text / thinking / tool_use / tool_result 各成一 turn)
  ↓ 客户端 Layer 0 正则脱敏(常见 key、token、email、IP、home path)
  ↓ HMAC-SHA256 签名 POST /v1/lite/push
  ↓ Gateway 反代到 FastAPI(8081)
  ↓ 服务端 Layer 1 正则脱敏(始终运行)
  ↓ 服务端 Layer 2 OPF 深度脱敏(EXP_DEFER_OPF=1 时跳过,后台 worker 补)
  ↓ 服务端 Layer 3 LLM 商业敏感判定(仅 Layer 1 高严重度命中时触发)
  ↓ 计算 trajectory fingerprint → 同 agent 重复上传命中已有 experience_id
  ↓ 写 experiences 表 + trajectory sidecar 文件 + vectors(intent embedding)
  ↓ 写 audit_log
  ↓ 返回 experience_id

检索:/v1/lite/search 取候选向量 → 按当前 viewer 身份做 ACL 过滤 → 余弦 top-k
```

MVP 暂不依赖评分、信用回流、技能市场。`publish` / `rewards` / `annotate` 接口已实现,
后续打开。

## 2. 身份认证

每个 Agent 有独立身份:

- `EXP_AGENT_NAME`:agent 名,如 `user-xhh666`
- `EXP_AGENT_SECRET`:64 hex HMAC secret
- `EXP_BASE_URL`:gateway URL,内网默认 `http://10.244.66.195:3080`

签名规则:

```text
signature = HMAC_SHA256(secret, METHOD + "\n" + PATH + "\n" + BODY_BYTES)
```

每次请求带:

```http
X-Agent-Name: <EXP_AGENT_NAME>
X-Signature: <hex-signature>
```

服务端用 `X-Agent-Name` 找 credential,校验签名。失败返回 `401 bad signature`。

## 3. 上传体结构

`POST /v1/lite/push`。**默认必须带 `trajectory`**(`EXP_REQUIRE_TRAJECTORY=1`)。

```json
{
  "query": "用户原始问题",
  "intent": "一句话任务标题",
  "steps": ["关键步骤 1", "关键步骤 2"],
  "outcome": "最终结果或最后一次助手回复",
  "task_type": "debugging",
  "source_model": "claude-code",
  "sensitivity": "medium",
  "acl": "private",
  "tags": ["auto-sync"],
  "redactions": {},
  "trajectory": [
    {"role": "user",      "content": "...", "ts": "2026-05-05T13:30:01Z", "tool_calls": [], "tool_result_for": ""},
    {"role": "assistant", "content": "💭 思考\n\n...", "ts": "..."},
    {"role": "assistant", "content": "...", "ts": "...", "tool_calls": [
      {"id": "toolu_01...", "name": "Bash", "input": {"command": "ls"}}
    ]},
    {"role": "tool", "content": "<full output, no truncation>", "ts": "...", "tool_result_for": "toolu_01..."}
  ],
  "system": null,
  "tools": null,
  "meta": {"agent_type": "claude-code", "session_id": "...", "cwd": "..."}
}
```

字段说明:

- `query`:第一条真实用户消息。`<environment_context>`、`<local-command-caveat>`、
  `<command-message>` 等 wrapper 会被跳过。
- `intent`:UI 卡片标题 + 检索主字段。优先级:
  1. trajectory 里最后出现的 `[task-summary]: <一行>` 标记
  2. 本地 `claude -p` LLM 总结(`EXP_REFINE_TITLES=1`,默认开)
  3. 第一条真实用户消息的第一句话(heuristic 兜底)
- `steps`:简短步骤摘要。MVP 中只展示,不单独 embedding。
- `outcome`:最后一次助手回复。
- `trajectory`:完整轨迹。每个 turn 是 `{role, content, ts, tool_calls, tool_result_for}`。
  - **每个 Anthropic content block 拆成独立 turn**:`text` / `thinking` / `tool_use` / `tool_result` 各一条,UI 才能分别气泡渲染。
  - **`thinking`** 块以 `💭 思考\n\n<plaintext>` 形式作为 assistant turn 上传。Claude API
    redacted_thinking 模式只有 base64 signature 没有 plaintext,这种 block 直接丢。
  - **`tool_use`**:assistant turn,`content=""`,`tool_calls=[{id, name, input}]`。
  - **`tool_result`**:`role="tool"`,`content` 是工具完整原文,`tool_result_for` 指向对应 `tool_use.id`。
  - **codex** 的 `function_call` / `function_call_output` / `reasoning` 全部映射到上面三种,统一 schema。
- `acl`:`private` / `team:<name>` / `public`,默认 `private`。

## 4. 客户端上传模式

### 模式 A:从 portal `/me` 复制 bind 命令(推荐)

1. 浏览器打开 `http://10.244.66.195:3080/login` 用 `xxx@sii.edu.cn` 邮箱登录
2. 进入 `/me` 页,复制「绑定本机」面板里的 curl 命令
3. 终端粘贴执行,一行装好:

```bash
curl -sSL http://10.244.66.195:3080/install | \
  EXP_AGENT_NAME='user-xxx' \
  EXP_AGENT_SECRET='<portal-issued-secret>' \
  EXP_BASE_URL='http://10.244.66.195:3080' \
  bash
```

`install.sh` 会做这些事:

1. 下载 `exp_uploader.py` / `exp_consent.py` / `exp_annotator.py` 到 `~/.experience-pool/bin/`
2. 写入 `~/.experience-pool/credentials/<name>.json`(0600)
3. 检测本机已装的 agent(claude-code / cursor / codex / hermes / openclaw),把
   `agent-contract.md` 分发到各自的 SKILL.md / `~/.cursor/rules/` / `~/.codex/AGENTS.md`
4. 给 Claude Code 写 `SessionEnd` + `SessionStart` hook(覆盖式重写,旧的实验性
   hook 会被识别并清掉)
5. 装 systemd user timer 或 launchd LaunchAgent,每 120 秒跑一次 `daemon-tick`
6. **不**自动 backfill 历史 session(`EXP_BACKFILL=1` 才跑);如果想后补:
   `bash ~/.experience-pool/run-backfill.sh &`

凭据落到 `~/.experience-pool/credentials/<agent-name>.json`,权限 0600。

### 模式 B:无凭据 install(register on demand)

不带 `EXP_AGENT_SECRET` 直接装,会触发 `exp register`,服务端发一份新凭据:

```bash
curl -sSL http://10.244.66.195:3080/install | \
  EXP_AGENT_NAME="$(whoami)-$(hostname -s)" \
  EXP_TEAM="videogen" \
  EXP_BASE_URL='http://10.244.66.195:3080' \
  bash
```

这种方式开出的 agent 不绑定到内网用户,后续不能在 `/me` 页管理。**生产不建议**,
只用于跑通流水线。

### 模式 C:session-extractor 历史回填(零依赖、私有)

不要求装 `exp` CLI,`acl` 在代码里硬编码 `private`。适合一台新机器一次性把
本地历史全捞上去:

```bash
curl -fsSL http://10.244.66.195:3080/session-extractor/run.sh | \
  EXP_AGENT_NAME='user-xxx' \
  EXP_AGENT_SECRET='<portal-issued-secret>' \
  EXP_BASE_URL='http://10.244.66.195:3080' \
  EXP_EXTRACTOR_FLAGS='--max-mb 0 --sleep 0 --verbose' \
  bash
```

常用 `EXP_EXTRACTOR_FLAGS`:

| flag | 作用 |
|---|---|
| `--max-mb 0` | 不限单 session 大小(默认 4 MB) |
| `--sleep 0` | 不在两次上传间停顿 |
| `--limit 200` | 最多上传 N 条 |
| `--sources claude-code,codex` | 只跑这些源 |
| `--since 2026-04-01` | 只跑这个日期之后的 |
| `--dry-run` | 看会上传什么不动手 |
| `--verbose` / `-v` | 每条 session 都打一行 |

服务端按 `(agent_id, fingerprint)` 去重,重复上传同样内容直接返回已有 `experience_id`,**重跑安全**。

> ⚠️ 撤回(revoke)**不删** `content_fingerprints` 表里的指纹。如果想撤回后重新上传同一份内容,
> 需要手动清: `DELETE FROM content_fingerprints WHERE experience_id=?`。

### 模式 D:任意 Agent 框架直接 API 上传

只要能拼上面 §3 的 JSON 就行:

```bash
BODY='{"query":"...","intent":"...","steps":["..."],"outcome":"...","task_type":"misc","source_model":"custom-agent","sensitivity":"medium","acl":"private","trajectory":[{"role":"user","content":"..."}]}'
SIG=$(printf 'POST\n/v1/lite/push\n%s' "$BODY" \
      | openssl dgst -sha256 -hmac "$EXP_AGENT_SECRET" | awk '{print $NF}')

curl -X POST "$EXP_BASE_URL/v1/lite/push" \
  -H "X-Agent-Name: $EXP_AGENT_NAME" \
  -H "X-Signature: $SIG" \
  -H "Content-Type: application/json" \
  -d "$BODY"
```

注意 `BODY` 字节必须和你签名时用的字节一致,不要被 shell 重新引号化。

## 5. 标题(intent)生成逻辑

上传时给 trace 起一个 UI 标题。优先级:

1. **`[task-summary]: <一行>`** —— trajectory 中**最后**一次出现的 `[task-summary]: ...` 标记。
   零成本(就是模型早就写在回复末尾的字符串),最准确。
2. **本地 `claude -p` LLM 总结**(默认开,`EXP_REFINE_TITLES=1`,可设 `=0` 关掉):
   - 把整段 transcript 压成 ≤6KB 的 `[用户]/[助手]/[工具结果]` 摘要
   - 调本机 `claude -p` 让它输出一行标题(动词+对象,中文 ≤25 字 / 英文 ≤8 词)
   - **必须用 `--no-session-persistence`** —— 否则子进程会写一个新 session 文件到
     `~/.claude/projects/`,被下次 daemon-tick 当成新 session 抓走,标题就成了
     `<transcript>`。一次踩坑能造出几百条假记录。
   - **子进程 env 必须含 `EXP_AUTO_UPLOAD=0` + `EXP_TITLE_DISABLE=1`** —— 否则它的
     `SessionEnd` hook 触发 `exp push-latest`,push 又调 LLM 总结,LLM 又起子进程……
     **无限递归**(亲测一次能造出 363 条 row)。
   - 模型偶尔会回 conversational(`I'll extract...` / `我看到...` 之类),
     `_looks_bad_title()` 会拒掉,落回 heuristic。
3. **第一条真实用户消息的第一句话**(`_derive_title_heuristic`):
   把 `<environment_context>` / `<local-command-caveat>` / `<command-message>` 跳过,
   取 `query` 第一行第一句、≤70 char。

Agent 实现侧建议:每完成一个子任务就在回复尾部带一行
`[task-summary]: <动词 + 对象>`,可以省掉一次 LLM 调用,标题质量也更好。

## 6. 脱敏

四层防线,自下而上:

| 层 | 在哪 | 是否始终运行 |
|---|---|---|
| L0 客户端正则 | `bin/exp_uploader.py:sanitize()` 上传前 | ✅ 始终 |
| L1 服务端正则 | `core/exp_core/lite.py:_walk()` push 时 | ✅ 始终 |
| L2 OPF 深度脱敏 | 服务端调 `EXP_OPF_REMOTE_URL` 远程 OPF | `EXP_DEFER_OPF=1` 时跳过,等 worker |
| L2 backfill worker | `scripts/exp_opf_worker.py`,扫 `layer1_only` 行补脱敏 | **必须单独启动** |
| L3 LLM 商业敏感判定 | `core/exp_core/sanitize.py:layer3_judge`,L1 高严重度命中时触发 | 视命中触发 |

L0 / L1 覆盖:

- Anthropic / OpenAI / GitHub / AWS / Stripe / Slack 等 API key
- Bearer token、JWT、URL credentials、数据库 URI
- 邮箱、手机号、身份证形状、IPv4、home path 用户名
- tool_calls 的 input / output **递归** 脱敏

服务端写 row 时:

- `sanitization_status='layer1_only'` —— L1 跑过,L2 待 worker 补
- `sanitization_status='done'` —— L2 也补完了
- `sanitization_status='flagged'` / `'human_review'` —— L1 命中高严重度,需要人工审核
- `redactions` —— 各层命中类别计数
- `strict_redactions` —— L2 OPF 命中(L2 没跑就是空)

### 启 OPF backfill worker

不启动的话,**所有行永远停在 `layer1_only`**。当前内网状态:1491 行 `layer1_only`,
0 行 `done`(因为 worker 没跑过)。启动方式:

```bash
EXP_OPF_REMOTE_URL=http://10.245.4.167:8085 \
EXP_DB_PATH=/tmp/exp-mvp/pool.db \
EXP_TRAJECTORIES_DIR=/tmp/exp-mvp/trajectories \
nohup python3 /inspire/hdd/.../experience-pool/scripts/exp_opf_worker.py \
  --interval 30 -v > /tmp/exp-mvp/opf-worker.log 2>&1 &
```

或者纳入 babysit(下面 §10)。worker 是幂等的,同一行不会被重复处理。

## 7. 服务端落库流程

`/v1/lite/push` 内部步骤:

1. HMAC 校验身份
2. 用 `agent_name` 找 `agent_id` 和 `team`
3. 算 trajectory fingerprint;同 agent 已存在则直接返回 existing `experience_id`
4. **L1 服务端正则脱敏**(始终)
5. **L2 OPF 深度脱敏**:
   - `EXP_DEFER_OPF=1` → 跳过,row 落地 `sanitization_status='layer1_only'`,等 worker 补
   - 否则同步调 OPF,`done`
6. L1 命中高严重度类别 → 触发 L3 LLM 判定 → 标 `flagged` / `human_review`
7. 写 `experiences`(query / intent_text / script_steps / outcome / acl / tags 等)
8. 写 trajectory sidecar 文件:`<EXP_ROOT>/trajectories/<eid>.json`
9. 写 `content_fingerprints` 用于 per-agent 去重
10. 对 `intent + query` 做 embedding,写 `vectors(kind='intent')`
11. 写 `audit_log`
12. 可选后台 auto-label(`EXP_AUTO_LABEL_ENABLED=1`,需要 OpenAI-compat endpoint)

检索 `/v1/lite/search`:

- 取候选向量,按 viewer 的身份做 ACL 过滤:
  - `private` —— 只返回当前 viewer 自己 owner/agent 的私有经验
  - `team:<name>` —— viewer 在同 team 才能看
  - `public` —— 全员可见,但社区入口受 publish 状态 + 解锁条件影响
- 排序按余弦相似度 top-k,MVP 不混合质量分数

## 8. 撤回 / 发布 / 个人页

默认上传都是 `private`。在 `/me` 页可以分页查看、撤回、发布。

CLI:

```bash
~/.experience-pool/bin/exp consent revoke --eid <experience_id> --reason user_request
~/.experience-pool/bin/exp publish        --eid <experience_id>
~/.experience-pool/bin/exp unpublish      --eid <experience_id>
~/.experience-pool/bin/exp quota
```

`/v1/lite/revoke` 实际做了:

1. 校验 caller 是 agent owner
2. **删 trajectory sidecar 文件**(磁盘上 `<EXP_ROOT>/trajectories/<eid>.json`)
3. 标记 `revoked=1`、`revoked_at=now`、`review_status='revoked'`、`trajectory_path=NULL`
4. `DELETE FROM vectors WHERE experience_id=?`(检索不再返回)
5. `DELETE FROM cluster_membership WHERE experience_id=?`
6. `DELETE FROM turn_rewards WHERE experience_id=?`
7. 写 `audit_log`
8. **不动 `content_fingerprints`** —— 想再传同样内容需要手动清

## 9. 常见问题

**`401 bad signature`**
检查 `EXP_AGENT_NAME` / `EXP_AGENT_SECRET` / `EXP_BASE_URL` 是否对应同一服务端。
签名 body 必须和实际发送 body 字节一致,不要被 shell 引号化二次转义。

**`trajectory is required`**
服务端默认 `EXP_REQUIRE_TRAJECTORY=1`。card-only 上传被拒。带上 `trajectory` 即可。

**网页 `/me` 只看到一部分**
`/me` 是分页的。`?page=2` / `?page=3` 翻。

**Next.js 在 vscode notebook proxy 下静态资源 404**
build 和 start 时必须设 `EXP_UI_PUBLIC_URL=<.../proxy/3002>`,然后重新 build/start。
中途换 base path 一定要清 `.next` 重建。

**标题不好(出现整段对话、`<transcript>`、`Waiting for...`)**
- 完成任务时让 agent 在回复尾部加一行 `[task-summary]: <动词 + 对象>`,优先用这个。
- 检查 `~/.claude/projects/-tmp/*.jsonl` 有没有大量小文件——这是 LLM 子进程没用
  `--no-session-persistence` 留下的 prober 残骸,被 daemon-tick 当真 session 抓走。
  当前版本已修;旧残骸不会被传(过滤了 query 以 `<transcript>` 开头的 session),
  但占磁盘可以手动 `rm`。

**上传量异常翻倍 / 同一 session 出现几十次副本**
title LLM 子进程触发 SessionEnd hook,递归。检查子进程 env 是否含
`EXP_AUTO_UPLOAD=0` + `EXP_TITLE_DISABLE=1`。当前代码已强制隔离。

**所有 row 永远 `layer1_only`,`strict_redactions` 永远空**
OPF backfill worker 没启动。看 §6 末尾的启动命令,或者把 worker 加到 babysit。

**撤回后重新上传被指纹挡住,服务端返回旧 eid**
`content_fingerprints` 没跟着撤回一起删。手动清:
```bash
sqlite3 /tmp/exp-mvp/pool.db "DELETE FROM content_fingerprints WHERE experience_id='<eid>'"
```

**门户 `/me` 无法登录**
内网邮箱必须以 `@sii.edu.cn` 结尾。注册时密码会用 PBKDF2-SHA256 600k 哈希,
登录走 cookie session。

**上传慢**
看是不是 OPF 同步调用。生产建议 `EXP_DEFER_OPF=1` + 启动 worker,主 API 不在
CPU 上跑重模型。

## 10. 部署前检查清单

- `curl -m 4 http://10.244.66.195:3080/healthz` 返回 200
- `curl -m 4 http://10.244.66.195:3080/v1/lite/healthz` 返回 OK 或 200
- 8081(FastAPI)、3080(gateway)、3002(Next.js)三个端口都通
- `~/.experience-pool/bin/exp whoami` 能读到正确 agent
- `~/.experience-pool/bin/exp push-latest --yes --acl private` 返回 `experience_id`
- `/me` 上能看到这条新记录,详情页能展开 trajectory(气泡视图 + JSON 视图)
- `~/.experience-pool/bin/exp search --q "<刚上传的任务>" --top-k 5` 搜得到
- `ps aux | grep exp_opf_worker` —— OPF backfill worker 在跑
- `ls ~/.claude/projects/-tmp/*.jsonl 2>/dev/null | wc -l` ≈ 0(没有新的 prober 残骸)
- `ps aux | grep babysit` —— 服务看护脚本在跑
- 文档、脚本、README 里没有真实 secret / OAuth token / API key
