# 详细说明书:把 Claude Code 会话上传到经验池

这一篇讲清楚:经验池**为什么**要这么设计、Claude Code 的 trace **从哪里来、长什么样**、**怎么一步步**变成数据库里一条可检索的经验记录。看完就能自己 debug 任何一步。

---

## 0. 你将看到的最终结果

走完一次完整上传后,在 `/me` 页面会有一张卡片:

```
┌─────────────────────────────────────────────────────────────┐
│ 修复 Claude 设置 hooks 配置问题                  [task=misc] │
│ acl=private  •  3min ago  •  18 turns  •  fp:8a3b...        │
│                                                             │
│ Q: ~/.claude/settings.json 里 hooks 报错怎么修              │
│ A: …(LLM 总结的 outcome)…                                   │
│                                                             │
│ [打开详情] [复制 ID] [发布到 community] [撤回]              │
└─────────────────────────────────────────────────────────────┘
```

点「打开详情」,trajectory 以气泡视图展开:用户消息蓝色靠右,助手回复白色靠左,
工具调用是青色折叠卡片(点开看 input JSON),工具返回是琥珀色折叠卡片(点开
看完整 output)。每一步都是从你那一次和 Claude 真实对话里来的。

---

## 1. 一次完整上传的全链路

```
┌─────────────────────────────────────────────────────────────────────────┐
│                            本机(发起方)                                │
│                                                                          │
│  ① Claude Code 生成 session 文件                                        │
│     ~/.claude/projects/<encoded-cwd>/<uuid>.jsonl                       │
│                                                                          │
│  ② SessionEnd hook 触发                                                 │
│     ~/.claude/settings.json hooks.SessionEnd                            │
│       → ~/.experience-pool/bin/auto_upload.sh                           │
│                                                                          │
│  ③ auto_upload.sh 检查 consent → exp push-latest                        │
│       → ~/.experience-pool/bin/exp_uploader.py                          │
│                                                                          │
│  ④ Adapter parse                                                        │
│     ClaudeCodeAdapter.parse() 把 .jsonl 拆成 Session 对象               │
│                                                                          │
│  ⑤ 客户端 Layer 0 正则脱敏                                              │
│     sanitize() / sanitize_node() 抹 secret / token / 邮箱 / IP …        │
│                                                                          │
│  ⑥ HMAC 签名                                                            │
│     HMAC_SHA256(secret, "POST\n/v1/lite/push\n<body-bytes>")            │
│                                                                          │
│  ⑦ HTTP POST → <EXP_BASE_URL>/v1/lite/push                              │
│       (EXP_BASE_URL = portal /me 给的 vscode notebook proxy URL)        │
└─────────────────────────────────────────────────────────────────────────┘
                                      │
                                      ▼
┌─────────────────────────────────────────────────────────────────────────┐
│                          服务端(经验池主机)                            │
│                                                                          │
│  ⑧ Gateway(local-gateway.mjs / Caddy)反代到 FastAPI 8081                │
│                                                                          │
│  ⑨ FastAPI 中间件用 X-Agent-Name 找 credential,验签                    │
│                                                                          │
│  ⑩ push_lite() 主流程                                                   │
│     a. compute_fingerprint(trajectory) → SHA256 of normalized turns     │
│     b. 查 content_fingerprints(agent_id, fingerprint)                   │
│        命中 → 返回已有 experience_id,这次不写库(去重)                  │
│     c. Layer 1 服务端正则脱敏(_walk 递归 trajectory + card)            │
│     d. EXP_DEFER_OPF=1 时:standardization_status='layer1_only'         │
│        否则:同步调 Layer 2 OPF(远程 GPU)                              │
│     e. Layer 1 命中高严重度 → 触发 Layer 3 LLM 业务敏感判定             │
│                                                                          │
│  ⑪ INSERT INTO experiences(query, intent_text, script_steps, …)        │
│      + 写 trajectory sidecar:                                           │
│        /tmp/exp-mvp/trajectories/<eid>.json                             │
│      + INSERT INTO content_fingerprints(fingerprint, agent_id, eid)    │
│      + 对 intent + query 做 embedding,写 vectors 表                     │
│      + 写 audit_log                                                      │
│                                                                          │
│  ⑫ FastAPI BackgroundTask (push 响应已经返回了):                        │
│      a. title_refine.refine_title_for_experience(eid)                  │
│         → 调本机 claude -p 改写 intent_text 成一行总结                   │
│      b. 如果 EXP_AUTO_LABEL_ENABLED=1,跑 reward 注释                    │
│                                                                          │
│  ⑬ 后台 OPF backfill worker(独立长跑进程):                             │
│      扫 sanitization_status='layer1_only' 的行,补跑 Layer 2 OPF        │
│                                                                          │
│  ⑭ 完成。/me 上能看到这条 row,/v1/lite/search 也能搜到                 │
└─────────────────────────────────────────────────────────────────────────┘
```

下面**每一步**都拆开讲。

---

## 2. 每一步:Claude Code 上传细解

### 步骤 ① — Claude Code session 文件长什么样

**位置**:`~/.claude/projects/<encoded-cwd>/<session-uuid>.jsonl`

`encoded-cwd` 是当前工作目录被路径编码,比如 `/inspire/hdd/.../experience-pool` 会变成
`-inspire-hdd-...-experience-pool`(把 `/` 换成 `-` 加前缀)。

**格式**:每行一个 JSON 对象,共有这些 `type`:

| type | 含义 |
|---|---|
| `user` | 用户输入(可能是字符串,也可能是 `[{type:"text", ...}]` / `[{type:"tool_result", ...}]`) |
| `assistant` | 助手回复(`message.content` 是 block 列表:`text` / `thinking` / `tool_use`) |
| `system` | hook duration / stop_reason 等元数据(不是 LLM 看到的内容) |
| `attachment` | hook 注入的内容(SessionStart 提示)、deferred_tools_delta(可调用工具更新) |
| `last-prompt` / `permission-mode` / `ai-title` / `queue-operation` / `file-history-snapshot` | IDE 内部状态 |

实际样本一条 user 行:

```json
{"type":"user","message":{"role":"user","content":"~/.claude/settings.json 里 hooks 报错怎么修"},
 "timestamp":"2026-05-05T10:30:00.000Z","sessionId":"...","uuid":"...","parentUuid":null}
```

一条 assistant 行(包含 thinking + tool_use):

```json
{"type":"assistant","message":{"role":"assistant","content":[
  {"type":"thinking","thinking":"要先看一下 settings.json 当前的样子","signature":"EroCC..."},
  {"type":"text","text":"我先读一下你的 settings.json"},
  {"type":"tool_use","id":"toolu_01xxx","name":"Read","input":{"file_path":"~/.claude/settings.json"}}
]},"timestamp":"...","sessionId":"...","uuid":"..."}
```

一条 user 行(其实是上一条 tool_use 的结果):

```json
{"type":"user","message":{"role":"user","content":[
  {"type":"tool_result","tool_use_id":"toolu_01xxx","content":"{\"hooks\": ...}"}
]},"timestamp":"...","sessionId":"...","uuid":"..."}
```

注意:**Claude API 把 tool_result 放在 user 消息里**,这是 Anthropic 协议的标准结构。
不是用户键入的"用户消息"。

### 步骤 ② — SessionEnd hook

`install.sh` 装好之后,`~/.claude/settings.json` 里有这一段:

```json
{
  "env": { "EXP_AUTO_UPLOAD": "1" },
  "hooks": {
    "SessionEnd": [{
      "matcher": "clear|logout|prompt_input_exit|bypass_permissions_disabled|other",
      "hooks": [{
        "type": "command",
        "command": "/root/.experience-pool/bin/auto_upload.sh"
      }]
    }],
    "SessionStart": [...]
  }
}
```

**触发时机**:每次 Claude Code session 结束(prompt 输入退出、`/clear`、`/logout` 等)。

`matcher` 里这些事件**实际上每次你完成回复都会发**(因为 prompt_input_exit 之后下一次输入会
重新进入 prompt)。所以一个长 session 里 hook 会被触发多次 —— 每次都 push 当前 jsonl,
依靠**指纹去重**避免同一 session 落多份(见步骤 ⑩b)。

### 步骤 ③ — auto_upload.sh 做什么

```bash
#!/usr/bin/env bash
set -eu
LOG_DIR="${HOME}/.experience-pool/logs"
LOG="$LOG_DIR/upload.log"

# 1. 没开总开关就退出
if [ "${EXP_AUTO_UPLOAD:-0}" != "1" ]; then
    exit 0
fi

# 2. 用 setsid + nohup detach,主进程立刻 exit 0,不阻塞 Claude Code
(
    setsid </dev/null >>"$LOG" 2>&1 bash -c '
        # 3. 问 consent module 这个 cwd / session 是否允许上传
        DECISION=$("$WRAPPER" consent decide \
                      --agent claude-code --cwd "$CWD" --session "$SID")
        if [ "$DECISION" = "upload" ]; then
            "$WRAPPER" push-latest --yes \
                --source claude-code \
                --task "${EXP_TASK:-misc}" \
                --sensitivity "${EXP_SENSITIVITY:-medium}" \
                --acl "${EXP_ACL:-private}"
        fi
    ' &
)
exit 0
```

关键:**异步 detach**。Claude Code hook 进程没有 TTY,等待会让 Claude 卡住。
所以脚本主体马上 `exit 0`,fork 出去的进程在后台继续。

### 步骤 ④ — Adapter parse:从 .jsonl 到 Session

代码在 `~/.experience-pool/bin/exp_uploader.py` 的 `ClaudeCodeAdapter.parse()`。

**任务**:把上面那一堆杂乱的 .jsonl 行变成一个 `Session` 对象,trajectory 字段是
有序的 `Turn` 列表。

#### 关键决策 1:每个 Anthropic content block 拆成独立 turn

源 JSONL 一行 assistant 可能含:`thinking` + `text` + `tool_use` × 3 个块。
旧版 adapter 把它们 join 进一个 `content` 字符串,信息密度太高、UI 渲染没法分别气泡。

新版:**每块出一个独立 turn**:

```python
for block in content:
    if block.type == "text":
        turns.append(Turn(role="assistant", content=block.text, ts=ts))
    elif block.type == "thinking":
        # 跳过 redacted_thinking(只有 base64 signature 没有 plaintext)
        if block.thinking:  # plaintext available
            turns.append(Turn(role="assistant",
                              content="💭 思考\n\n" + block.thinking, ts=ts))
    elif block.type == "tool_use":
        turns.append(Turn(role="assistant", content="",
                          tool_calls=[{"id":..., "name":..., "input":...}]))

# user message 里的 tool_result 单独成一条 role=tool 的 turn
for block in user_content:
    if block.type == "tool_result":
        turns.append(Turn(role="tool", content=block.content,
                          tool_result_for=block.tool_use_id))
    elif block.type == "text":
        turns.append(Turn(role="user", content=block.text))
```

#### 关键决策 2:跳过元数据 type

```python
SKIP_TYPES = {"system", "attachment", "last-prompt", "permission-mode",
              "ai-title", "queue-operation", "file-history-snapshot"}
if d.get("type") in SKIP_TYPES:
    continue
```

这些不是模型对话内容 —— 比如 `attachment` 里的 SessionStart 注入是 hook 输出,不是用户输入。

#### 关键决策 3:redacted_thinking 怎么办

Claude API 在 redacted-thinking 模式下,thinking block 的 `thinking` 字段是空字符串,
只有 `signature` 有 base64 数据。我们**直接丢**,因为没有可读 plaintext。

这意味着如果你的 session 里大部分 thinking 是 redacted,trajectory 里就看不到思考过程。
是 Claude API 设计限制,不是我们能控制的。

#### 关键决策 4:`[Request interrupted by user]` 当前是被过滤掉的

代码里有 `if not body.startswith("[Request interrupted")`。这是个**保守选择**——
中断本身有信号(用户改主意),但当前代码丢了。要保留可以改 adapter。

#### 实测:一个真实 session 拆出来的样子

源:1 个 1539-行 的 .jsonl(claude-code session)

```
- 1539 个 record
  - 1539 个 user/assistant 类型(其它过滤)
- 拆成 turn 之后:
  - 89 个真用户消息(role=user, content=string)
  - 282 个 assistant 文字回复
  - 0   个 thinking turn(全是 redacted_thinking)
  - 835 个 tool_use(role=assistant, content="", tool_calls=[...])
  - 833 个 tool_result(role=tool, content=<完整原文>, tool_result_for=...)
- 总:2039 turn
```

### 步骤 ⑤ — 客户端 Layer 0 脱敏

代码 `~/.experience-pool/bin/exp_uploader.py` 里的 `sanitize()` / `sanitize_node()`。
读 `core/exp_core/sanitize_rules.yaml`(同一份规则),在每个字符串叶子上跑一遍正则。

#### 三个地方都跑:

1. `turn.content` —— 文字内容
2. `turn.tool_calls[i].input` —— 工具参数,**递归**进 dict / list 任意深度
3. tool_result 的 `content` —— 工具输出

#### 高严重度规则一览(命中即触发服务端人工审):

```yaml
- pem_private_key   → <PRIVATE_KEY>
- ssh_pubkey        → <SSH_KEY>
- aws_access_key    → <KEY>     # AKIA / ASIA + 16字符
- jwt               → <JWT>     # eyJ... 三段
- bearer_token      → Bearer <TOKEN>
- anthropic_key     → <SECRET>  # sk-ant-...
- openai_key        → <SECRET>  # sk-... (不是 sk-ant- / sk-proj-)
- xai/groq/google/hf/stripe/github/gitlab/npm/vercel/supabase/cloudflare/sentry/slack token
- generic_api_key   → key=<SECRET>  # api_key=... password=... 等
- url_with_credentials → scheme://<USER>:<PASS>@host
- db_uri              → scheme://<DB_URI>
- idcard_cn         → <ID_CARD>  # 18位身份证
- credit_card       → <CARD>     # Luhn 验证
```

#### 中严重度:

```yaml
- email      → <EMAIL>
- phone_intl → <PHONE>
- phone_cn   → <PHONE>   # 1[3-9]xxxxxxxxx
```

#### 低严重度:

```yaml
- ipv4 / ipv6 → <IP>
- home_path  → <HOMEDIR>/   # /Users/<x>/ 或 /home/<x>/
```

#### 双保险

服务端步骤 ⑩c 跑**同一份**规则。哪怕客户端被改坏 / 跳过,raw secret 也会在落库前被服务端再抹一次。

### 步骤 ⑥ — HMAC 签名

```python
canonical = b"POST\n/v1/lite/push\n" + body_bytes
signature = hmac.new(secret.encode(), canonical, hashlib.sha256).hexdigest()
```

凭据从 `~/.experience-pool/credentials/<agent-name>.json` 读:

```json
{
  "agent_id": "uuid",
  "agent_name": "user-xhh666",
  "team": "default",
  "secret": "64-hex-chars"
}
```

文件 `0600`,只本机能读。secret 是 portal 在用户登录后通过 `/v1/users/me/bind-script`
端点发的(用户复制 curl 命令时已经包含了)。

### 步骤 ⑦ — HTTP POST

```http
POST <EXP_BASE_URL>/v1/lite/push HTTP/1.1
Content-Type: application/json
X-Agent-Name: user-xhh666
X-Signature: <hex-signature>

{"query":"...","intent":"...","trajectory":[...], ...}
```

进 gateway。

### 步骤 ⑧ — Gateway

`scripts/local-gateway.mjs`(开发)或 `deploy/Caddyfile`(生产)做的事:

- `/v1/*` → FastAPI 8081
- `/install`、`/install.sh`、`/exp_uploader.py`、`/session-extractor/*` 等 bootstrap 路径
  → 直接 serve 静态文件(从 `dist-public/`)
- 其它 → Next.js UI 3002

为什么需要 gateway:Next.js 必须前缀绑定的路径配置(`basePath`)和 FastAPI 的 `/v1/*`
要在同一个端口上能区分,这就是 gateway 的工作。

### 步骤 ⑨ — FastAPI 验签中间件

```python
# core/exp_core/server.py
@app.middleware("http")
async def hmac_auth(request, call_next):
    if request.url.path.startswith("/v1/users/") or path == "/healthz":
        return await call_next(request)  # 用户登录路径走 cookie session,不验 HMAC
    name = request.headers.get("X-Agent-Name")
    sig  = request.headers.get("X-Signature")
    body = await request.body()
    cred = pool.lookup_credential(name)
    expected = hmac.new(cred.secret, f"{method}\n{path}\n".encode() + body, sha256).hexdigest()
    if not hmac.compare_digest(sig, expected):
        return JSONResponse({"error":"bad signature"}, status_code=401)
    request.state.agent_name = name
    return await call_next(request)
```

签名失败 → 401。成功 → 把 `agent_name` 存到 `request.state`,下游 endpoint 用。

### 步骤 ⑩ — push_lite 主流程

`core/exp_core/lite.py:push_lite()`。这是整个上传**最核心**的函数。

#### 步骤 ⑩a — 计算 fingerprint

```python
fingerprint = quality.compute_fingerprint(trajectory)
# = SHA256 of canonical-form trajectory:
#   每个 turn 序列化成 (role, normalized_content, tool_call_names_only)
#   然后 join 起来 sha256
```

为什么这么算 —— 同一个 .jsonl 在不同时刻被 hook 抓取上传(SessionEnd 触发了 N 次):
- content 不变(文件没变)
- 但 timestamp / metadata 等浮动字段会变
所以 fingerprint **只看实质对话内容**。

#### 步骤 ⑩b — 指纹去重

```python
existing = conn.execute(
    "SELECT experience_id FROM content_fingerprints "
    "WHERE fingerprint=? AND agent_id=?",
    (fingerprint, agent_id),
).fetchone()
if existing:
    return {"experience_id": existing["experience_id"], "deduplicated": True}
```

**作用域是 per-agent**,不是全局:
- 同一用户上传同样内容 → 命中,返回旧 eid,不写库
- 不同用户上传同样内容(罕见,理论上每个人对话独立)→ 各自有自己的 row

⚠️ 撤回(revoke)操作**不**清这张表里的记录。撤回后想重传同样内容会被指纹挡住,
返回旧 eid。要绕开就 `DELETE FROM content_fingerprints WHERE experience_id='<eid>'`。

#### 步骤 ⑩c — Layer 1 服务端正则

```python
def _walk(node):
    if isinstance(node, str):
        return layer1_text(node, rules)  # 跑 sanitize_rules.yaml 所有规则
    if isinstance(node, dict):
        return {k: _walk(v) for k, v in node.items()}
    if isinstance(node, list):
        return [_walk(x) for x in node]
    return node

clean_trajectory = _walk(trajectory)
clean_card = _walk(card.to_dict())
clean_system = _walk(system)
clean_tools = _walk(tools)
```

返回 `(cleaned, redactions_count)`。`redactions_count` 是 dict,key 是规则名(`email`、
`ipv4`、`anthropic_key` 等),value 是命中次数。

#### 步骤 ⑩d — Layer 2 OPF 决策

```python
defer_opf  = os.getenv("EXP_DEFER_OPF", "0") == "1"
remote_url = os.getenv("EXP_OPF_REMOTE_URL", "")
opf_on     = (not defer_opf) and (not remote_url) and opf_filter.is_enabled()

if defer_opf:
    sanitization_status = "layer1_only"
elif remote_url:
    # 同步调远程 OPF
    response = post_to_opf(remote_url, clean_trajectory)
    if response.ok:
        clean_trajectory = response["redacted"]
        sanitization_status = "done"
        strict_redactions = response["counts"]
elif opf_on:
    # 进程内跑(老路径,慢)
    clean_trajectory = opf_filter.redact_trajectory(clean_trajectory)
    sanitization_status = "done"
else:
    sanitization_status = "layer1_only"  # 没有 OPF 可用,跟 defer 一样
```

当前内网 `EXP_DEFER_OPF=1`,OPF 永远走 backfill worker。

#### 步骤 ⑩e — Layer 3 LLM 业务敏感判定

仅当 Layer 1 命中**高严重度**类别时触发:

```python
if has_high_severity(redactions_count):
    layer3_result = llm.call_json(LAYER3_PROMPT + "\n\n" + str(clean_trajectory))
    # = {"is_sensitive": bool, "categories": [...], "rationale": "..."}
    if layer3_result["is_sensitive"]:
        review_status = "human_review"
    else:
        review_status = "flagged"  # 命中过 high 但 LLM 判 not sensitive
else:
    review_status = "auto_approved"
```

当前内网 `EXP_LLM=mock`,L3 永远返回 not sensitive。等价于"L1 没命中 high → auto_approved,
L1 命中 high → flagged"。

### 步骤 ⑪ — 写库

```sql
INSERT INTO experiences (
    experience_id,    -- uuid4
    agent_id,         -- 来自 credential lookup
    task_type,        -- card.task_type
    source_model,     -- card.source_model
    query,            -- card.query
    intent_text,      -- card.intent
    script_steps,     -- json.dumps(card.steps)
    outcome,          -- card.outcome
    summary,          -- 同 outcome(MVP 简化)
    sensitivity,      -- card.sensitivity
    acl,              -- 'private' | 'team:X' | 'public'
    tags,             -- json.dumps(card.tags)
    sanitization_status, -- 'layer1_only' | 'done' | 'flagged' | 'human_review'
    review_status,       -- 'auto_approved' | 'pending' | ...
    extraction_status,   -- 'done'
    ingest_path,         -- 'lite'
    trajectory_path      -- 路径见下
);

INSERT INTO content_fingerprints (fingerprint, experience_id, agent_id, created_at);

INSERT INTO vectors (experience_id, kind, payload, embedding) VALUES (?, 'intent', ?, ?);
-- embedding = embed(intent_text + " " + query)

INSERT INTO audit_log (actor, actor_kind, action, target_id, payload);
```

trajectory sidecar 文件:

```python
traj_path = trajectories_dir / f"{eid}.json"
traj_path.write_text(json.dumps({
    "trajectory": clean_trajectory,
    "meta": {"agent_type", "session_id", "started_at", "ended_at", "cwd", ...}
}, ensure_ascii=False, indent=2))
# 默认 trajectories_dir = /tmp/exp-mvp/trajectories/
```

UI 详情页用 `trajectory_path` 把这个文件读回来气泡渲染。

### 步骤 ⑫ — push 后的 BackgroundTask

push 响应已经 `return result` 给客户端了,但 FastAPI 把这两个任务挂到了 `background_tasks`:

#### a. title_refine

```python
# core/exp_core/title_refine.py
def refine_title_for_experience(db_path, eid):
    row = db.execute("SELECT trajectory_path FROM experiences WHERE experience_id=?", (eid,))
    traj = json.load(open(row.trajectory_path))["trajectory"]
    transcript = pack_transcript(traj)  # 压成 ≤6KB 的 [用户]/[助手]/[工具结果] 摘要

    # 关键:子进程 env 必须隔离,否则 SessionEnd hook 递归
    env = dict(os.environ)
    env["EXP_AUTO_UPLOAD"] = "0"      # 子进程的 SessionEnd 是 no-op
    env["EXP_TITLE_DISABLE"] = "1"    # 防 nested refine
    proc = subprocess.run([
        "claude", "-p", "--output-format", "json",
        "--model", "claude-haiku-4-5-20251001",
        "--append-system-prompt", _TITLE_SYSTEM,
        "--no-session-persistence",  # 关键:不写 ~/.claude/projects 否则会被 daemon-tick 抓走当真 session
        "--disable-slash-commands",
    ], input=transcript, env=env, cwd="/tmp")

    label = parse_response(proc.stdout)
    label = strip_hook_noise(label)   # 去掉 SessionStart 注入的 "📥 connected to..." 行
    if not _looks_bad_title(label):   # 拒掉整段对话/conversational
        db.execute("UPDATE experiences SET intent_text=? WHERE experience_id=?", (label, eid))
```

#### b. auto_label(可选)

`core/exp_core/auto_label.py`,需要 `EXP_AUTO_LABEL_*` env 配 OpenAI-compat endpoint。

### 步骤 ⑬ — OPF backfill worker

独立长跑进程 `scripts/exp_opf_worker.py`:

```python
while True:
    rows = db.execute(
        "SELECT experience_id, trajectory_path FROM experiences "
        "WHERE sanitization_status='layer1_only' LIMIT 50"
    )
    for row in rows:
        traj = json.load(open(row.trajectory_path))
        cleaned, counts = post_to_opf(EXP_OPF_REMOTE_URL, traj)
        json.dump({"trajectory": cleaned, ...}, open(row.trajectory_path, "w"))
        db.execute(
            "UPDATE experiences SET sanitization_status='done', strict_redactions=? "
            "WHERE experience_id=?",
            (json.dumps(counts), row.experience_id),
        )
    sleep(30)
```

**当前内网这个 worker 没在跑**。所以全库 row 都停在 `layer1_only`,`strict_redactions` 全空。
启动方式:

```bash
EXP_OPF_REMOTE_URL=http://10.245.4.167:8085 \
EXP_DB_PATH=/tmp/exp-mvp/pool.db \
EXP_TRAJECTORIES_DIR=/tmp/exp-mvp/trajectories \
nohup python3 /inspire/hdd/.../scripts/exp_opf_worker.py --interval 30 -v \
  > /tmp/exp-mvp/opf-worker.log 2>&1 &
```

---

## 3. 实操:手动跑一次完整流程

如果上面的链路里某一步坏了想 debug,逐步手动跑:

### 准备凭据

```bash
# 拿 secret(从 portal /me 复制 bind 命令,里面有 EXP_AGENT_SECRET)
export EXP_AGENT_NAME='user-xxx'
export EXP_AGENT_SECRET='<hex-from-portal>'
export EXP_BASE_URL='<EXP_BASE_URL>'

# 写到本地 credential
mkdir -p ~/.experience-pool/credentials
cat > ~/.experience-pool/credentials/$EXP_AGENT_NAME.json <<EOF
{"agent_id":"$(uuidgen)","agent_name":"$EXP_AGENT_NAME","team":"default","secret":"$EXP_AGENT_SECRET"}
EOF
chmod 600 ~/.experience-pool/credentials/$EXP_AGENT_NAME.json
```

### 步 ④ — 看 adapter 把 jsonl 拆成什么

```python
import sys
sys.path.insert(0, '/root/.experience-pool/bin')
from exp_uploader import ClaudeCodeAdapter
from pathlib import Path

session = ClaudeCodeAdapter.parse(Path("~/.claude/projects/-foo/abc.jsonl").expanduser())
print(f"turns: {len(session.trajectory)}")
for t in session.trajectory[:5]:
    print(f"  [{t.role}] {(t.content or '')[:80]}")
    if t.tool_calls:
        print(f"    tool_calls: {[tc['name'] for tc in t.tool_calls]}")
```

### 步 ⑤ + ⑥ + ⑦ — 真上传

```bash
# 用 daemon-tick 跑一条最新的 claude-code session
~/.experience-pool/bin/exp daemon-tick \
    --max-per-source 1 --max-session-kb 32768 \
    --acl private -v
```

输出会有(成功):

```json
{"session": "1e03ccdb-...", "agent_type": "claude-code",
 "experience_id": "8f6e038f-...",
 "review_status": "auto_approved",
 "sanitization_status": "layer1_only",
 "redactions": {"ipv4": 12, "email": 3, "home_path": 5}}
```

### 步 ⑩ + ⑪ — 看服务端落地了什么

```bash
sqlite3 /tmp/exp-mvp/pool.db <<'SQL'
SELECT experience_id, intent_text, sanitization_status,
       review_status, length(trajectory_path) AS tp_len
FROM experiences
WHERE agent_id IN (SELECT agent_id FROM agents WHERE owner='user-xxx')
ORDER BY created_at DESC LIMIT 3;
SQL

# 看 trajectory 文件
ls -la /tmp/exp-mvp/trajectories/8f6e038f-*.json
python3 -c "
import json
data = json.load(open('/tmp/exp-mvp/trajectories/8f6e038f-...json'))
print(f'turns: {len(data[\"trajectory\"])}')
for t in data['trajectory'][:5]:
    print(f'  [{t[\"role\"]}] {t[\"content\"][:80]}')
"
```

### 步 ⑫a — 触发 title 改写(调试用)

```bash
# 直接调函数,绕过 BackgroundTask
cd /inspire/hdd/.../experience-pool
core/.venv/bin/python3 -c "
import sys; sys.path.insert(0, 'core')
from exp_core import title_refine
res = title_refine.refine_title_for_experience(
    '/tmp/exp-mvp/pool.db', '8f6e038f-...')
print(res)
"
```

### 步 ⑬ — 跑 OPF backfill

```bash
EXP_OPF_REMOTE_URL=http://10.245.4.167:8085 \
EXP_DB_PATH=/tmp/exp-mvp/pool.db \
EXP_TRAJECTORIES_DIR=/tmp/exp-mvp/trajectories \
python3 /inspire/hdd/.../scripts/exp_opf_worker.py --once -v
# --once 跑一轮就退,debug 用
```

跑完去 sqlite 查:

```sql
SELECT sanitization_status, COUNT(*) FROM experiences GROUP BY sanitization_status;
-- 现在应该看到 done 的数量增加,layer1_only 减少
```

---

## 4. 排错速查

### 客户端起不来

| 现象 | 原因 | 修法 |
|---|---|---|
| `exp whoami` 报 no credential | 没 bind | portal `/me` 复制 curl 重装 |
| `401 bad signature` | secret 跟 server 不匹配 | 检查 `EXP_BASE_URL`、credential 文件、PATH 大小写 |
| `connect refused` | gateway 没起 / 端口错 | `curl -m 4 $EXP_BASE_URL/healthz` 确认 |
| `trajectory is required` | 服务端 `EXP_REQUIRE_TRAJECTORY=1` | 上传时带 `trajectory` 字段 |

### Trace 看着不对

| 现象 | 在哪 |
|---|---|
| 标题成 `<transcript>` | step ⑫a 子进程没用 `--no-session-persistence`,落了 prober 文件被下次抓走。当前已修。旧残骸看 `~/.claude/projects/-tmp/*.jsonl`。 |
| 同 session 出现几十个副本 | step ⑫a 子进程 env 没 `EXP_AUTO_UPLOAD=0`,SessionEnd hook 递归。已修。 |
| trajectory 没有 thinking | source 是 redacted_thinking 模式,Claude API 不返 plaintext。无解。 |
| tool_result 在 user turn 里没拆出来 | 旧 adapter,升级到当前版本(每块独立 turn)。 |

### 数据库状态

| sanitization_status | 含义 | 修法 |
|---|---|---|
| `layer1_only` 卡住 | OPF worker 没启动 | step ⑬ 启动 worker |
| `human_review` 越来越多 | 设计:命中高严重度要审 | 在 admin 页 review |
| `flagged` | L3 判 not sensitive 但 L1 命中过 high | 不用管 |

### 撤回 + 重传

```bash
# 撤回
~/.experience-pool/bin/exp consent revoke --eid <eid> --reason 'redo'

# 撤回不删 fingerprint —— 重传会被挡住
sqlite3 /tmp/exp-mvp/pool.db "DELETE FROM content_fingerprints WHERE experience_id='<eid>'"

# 现在可以重传了
~/.experience-pool/bin/exp push --session <local-session-id> --acl private
```

---

## 5. 一张表速查所有环境变量

### 客户端

| env | 默认 | 作用 |
|---|---|---|
| `EXP_AGENT_NAME` | `$USER-$(hostname -s)` | 当前 agent 标识 |
| `EXP_AGENT_SECRET` | 从 credential 文件读 | bind 时 portal 注入 |
| `EXP_BASE_URL` | `<EXP_BASE_URL>` | gateway URL |
| `EXP_AUTO_UPLOAD` | `0`(install.sh 设 `1`) | SessionEnd hook 总开关 |
| `EXP_AUTO_SOURCES` | `claude-code,hermes,continue-dev,codex,agents-chat` | daemon-tick 跑哪些 source |
| `EXP_AUTO_ACL` | `private` | daemon-tick 默认 acl |
| `EXP_REFINE_TITLES` | `1` | 上传时调本地 LLM 改 title(客户端) |
| `EXP_TITLE_DISABLE` | `0` | 子进程隔离用 |
| `EXP_INSTALL_OPF` | `0` | install.sh 是否装客户端 OPF(~3GB) |
| `EXP_BACKFILL` | `0` | install.sh 是否自动批量回填 |

### 服务端

| env | 默认 | 作用 |
|---|---|---|
| `EXP_ROOT` | `/tmp/exp-mvp` | DB + trajectory 文件根目录 |
| `EXP_DB_PATH` | `$EXP_ROOT/pool.db` | sqlite 路径 |
| `EXP_LLM` | `claude` | L3 + extractor 用啥 LLM(`mock` / `claude`) |
| `EXP_LLM_MODEL` | `claude-haiku-4-5-20251001` | 调 claude CLI 时用哪个模型 |
| `EXP_REQUIRE_TRAJECTORY` | `1` | push 必须带 trajectory |
| `EXP_DEFER_OPF` | `0` | 1 = 跳过同步 OPF,等 backfill worker |
| `EXP_OPF_REMOTE_URL` | 空 | OPF 远程服务 URL,空 = 进程内跑(慢) |
| `EXP_AUTO_LABEL_ENABLED` | `0` | reward 注释开关 |
| `EXP_AUTO_LABEL_BASE_URL` / `_API_KEY` / `_MODEL` | 空 | reward 注释 OpenAI-compat endpoint |
| `EXP_REFINE_TITLE_SERVER` | `1` | 服务端 push 后 LLM 改 title |
| `EXP_TITLE_MODEL` | `claude-haiku-4-5-20251001` | title 改写用哪个模型 |
| `EXP_RATE_LIMIT_ENABLED` | `1` | rate limiter 开关 |
| `EXP_BIND_BASE_URL` | `EXP_BASE_URL` 同值 | portal `/me` bind 命令里写的 base |
| `EXP_PROFILE_PUSH` | `0` | push 时打 profile 日志 |
| `EXP_REFINE_TITLES` | `1` | 客户端 push-latest 时调 LLM(混合用,服务端无效) |

### OPF backfill worker

| env | 必填 |
|---|---|
| `EXP_OPF_REMOTE_URL` | ✅ OPF 服务 URL |
| `EXP_DB_PATH` | ✅ |
| `EXP_TRAJECTORIES_DIR` | ✅ |
| `EXP_OPF_AUTH_TOKEN` | 可选,OPF 服务的 auth token |
| `EXP_OPF_TIMEOUT_SECONDS` | 默认 60 |
| `EXP_OPF_WORKER_INTERVAL` | 默认 15 |

---

读到这里你应该能从「为什么 SessionEnd hook 触发了」一直追到「数据库里这条 row 怎么落地的」,
任何一步坏了都知道去哪查。

关联文档:

- [`docs/UPLOAD_LOGIC_AND_MANUAL.md`](UPLOAD_LOGIC_AND_MANUAL.md) — API 字段契约 + 部署清单
- [`docs/SANITIZATION.md`](SANITIZATION.md) — 四层脱敏每条规则细解
- [`agent-contract.md`](../agent-contract.md) — agent 怎么用经验池(给 LLM 看的契约)
- [`SKILL.md`](../SKILL.md) — Claude Code skill 入口
