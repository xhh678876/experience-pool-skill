# 插件 / 下游开发者接入说明书

写一个 Claude Code skill / Cursor command / 任意 agent 的"经验池查询"插件,
你需要的全部接口都在这。

> 服务端入口: `http://10.244.66.195:3080`(内网) 或 portal `/me` 给的 vscode
> proxy URL(其它内网电脑必须用 proxy URL,见 `docs/UPLOAD_LOGIC_AND_MANUAL.md` §4)

---

## 1. 三种接入路径

| 路径 | 给谁用 | 优势 |
|---|---|---|
| **A. 命令行调 `exp` CLI** | shell / 简单脚本 / claude code skill 的 bash hook | 零代码,零依赖,装完即可用 |
| **B. HTTP API + HMAC** | 任何语言的插件 | 跨语言,无需装 CLI |
| **C. 直接 import `exp_uploader.py`** | Python 插件 | 单文件 stdlib only,引入零依赖 |

---

## 2. 路径 A:`exp` CLI 全命令清单

装好之后(`curl -sSL <BASE>/install | EXP_AGENT_NAME=... EXP_AGENT_SECRET=... bash`),
`~/.experience-pool/bin/exp` 提供:

### 2.1 身份 / 状态

```bash
exp whoami                                    # 我是谁,base url 是什么
exp register --name foo --team bar            # 主动注册新身份(普通用户走 portal bind 不用这个)
exp bind                                      # 把 portal 给的 secret 写到本机 credential
exp quota                                     # 我的 publish_count / community 解锁状态
exp quota --json                              # 同上, JSON 输出
```

### 2.2 ⭐ 查询(插件主用)

```bash
# 语义搜索经验池(核心操作)
exp search --q "FastAPI HMAC 签名失败" --top-k 5
exp search --q "..." --scope auto             # 默认: personal + community
exp search --q "..." --scope personal         # 只看自己
exp search --q "..." --scope community        # 只看 community
exp search --q "..." --task-type debugging    # 限定 task_type
exp search --q "..." --json                   # 给脚本解析

# 拿单条经验详情
exp get --eid <experience_id>                 # 卡片 + steps + outcome
exp get --eid <eid> --include-trajectory      # 带完整 turn 列表(气泡渲染所需)
exp get --eid <eid> --json

# 列出本人 personal pool 全部
exp list                                      # 默认 50 条
exp list --limit 200 --json
exp ls                                        # alias

# Skills 库(MVP 阶段, 大部分情况返回空)
exp skills-search --q "deploy fastapi" --top-k 5
exp skills-install --name <skill-name> --target ./vendor/skills/foo
```

### 2.3 上传

```bash
# 看本机有哪些 session 可上传
exp list-sessions --source claude-code -v
exp list-sessions --source codex
exp list-sessions --source auto               # 自动检测

# 上传
exp push --session <id-or-prefix> --acl private              # 单条
exp push-latest --yes --task debugging                       # 当前 source 最新的
exp push-all --source claude-code --since 2026-04-01         # 批量
exp push-file --file traj.json --task csv_analysis           # 任意 JSON 形状

# 批量回填 (后台同步) — 一般 install.sh 已配 systemd timer 跑这个
exp daemon-tick --max-per-source 5 --acl private -v
exp daemon-tick --dry-run -v                  # 看会传啥不动手
exp daemon-state                              # 各 source 同步进度
exp daemon-reset                              # 忘掉同步记录, 下次 tick 全扫
exp daemon-reset --source claude-code         # 只重置一个 source
```

### 2.4 撤回 / 发布

```bash
exp revoke --eid <eid> --reason 'redo'        # 撤回(等同 consent revoke)
exp consent revoke --eid <eid>                # 老路径,等价

exp publish --eid <eid>                       # 私 → 公 (strict-public 审查会先跑)
exp unpublish --eid <eid>                     # 公 → 私 (publish_count 不会减)
```

### 2.5 评分(Synergy reward,可选)

```bash
exp annotate-existing --experience-id <eid>
exp annotate-existing --experience-id <eid> --annotate-model claude-haiku-4-5
exp get-rewards --experience-id <eid>
```

### 2.6 上传策略(consent)

```bash
exp consent show                              # 当前 consent.json
exp consent set --mode always|never|ask       # 全局
exp consent set --agent claude-code --mode always
exp consent set --cwd '~/work/clients/**' --mode never
exp consent set --session <id> --mode never   # 只对这个 session
exp consent reset                             # 清空回默认 (ask)
exp consent decide --agent claude-code --cwd "$PWD" --session "$SID"
                                              # → upload | skip(给 hook 用)
exp consent pending                           # 看被 skip 但留底的 session
```

### 2.7 运维 / 看板

```bash
exp opf-status                                # OPF backfill worker 状态
exp dashboard                                 # 全局 push 量 / 用户数 / sanitize 计数
```

每个命令都接 `--json` 给脚本解析(默认人类可读,加 `--json` 后 stdout 是 JSON)。
所有命令的 base URL 默认从 `EXP_BASE_URL` 环境变量读,凭据从
`~/.experience-pool/credentials/<name>.json` 读。

---

## 3. 路径 B:HTTP API + HMAC

所有 `/v1/*` 都验 HMAC 签名(除了 `/v1/users/*` 走 cookie session)。

### 3.1 签名公式

```
canonical_string = METHOD + "\n" + PATH + "\n" + REQUEST_BODY_BYTES
signature = hex(HMAC_SHA256(secret, canonical_string))
```

- `METHOD` 是 `GET` / `POST` 等大写
- `PATH` 是带前导 `/` 的路径(不含 query string;query 不参与签名)
- `REQUEST_BODY_BYTES` 是 HTTP body 的 raw 字节,GET 请求传**空字节串**
- `signature` 是 64 hex 字符

每次请求带:

```http
X-Agent-Name: <agent_name>
X-Signature: <hex-signature>
Content-Type: application/json
```

### 3.2 各端点速查

| Method | Path | Body / Query | 返回 |
|---|---|---|---|
| GET  | `/healthz` | — | `{"status":"ok",...}` |
| POST | `/v1/agents/register` | `{name, team}` | `{agent_id, secret}` |
| GET  | `/v1/users/me` | cookie | 当前 session 用户 |
| GET  | `/v1/users/me/bind-script` | cookie | bind 命令 + extract 命令 |
| GET  | `/v1/me/quota` | — | 个人 publish quota |
| **POST** | **`/v1/lite/search`** ⭐ | `{q, top_k, scope, task_type?}` | personal + community 结果 |
| **GET**  | **`/v1/experiences/<eid>`** ⭐ | `?include_trajectory=1` | 单条经验 |
| GET  | `/v1/experiences/search` | `?q=&top_k=` | 老 search 端点(等价于 lite/search) |
| POST | `/v1/lite/push` | LiteCard payload | `{experience_id, sanitization_status, ...}` |
| POST | `/v1/lite/revoke` | `{experience_id, reason}` | `{ok, status, deleted_files}` |
| POST | `/v1/lite/publish` | `{experience_id}` | `{ok, blocked_by?, hits?}` |
| POST | `/v1/lite/unpublish` | `{experience_id}` | `{ok}` |
| POST | `/v1/lite/rewards` | rewards JSON | `{accepted}` |
| GET  | `/v1/lite/rewards/<eid>` | — | per-turn rewards |
| GET  | `/v1/skills/search` | `?q=&top_k=` | skills 列表 |
| GET  | `/v1/skills/install` | `?name=` | skill 内容 |
| GET  | `/v1/admin/dashboard` | — | 全局指标 |
| GET  | `/v1/admin/opf-status` | — | OPF worker 状态 |
| GET  | `/v1/admin/leaderboard` | — | 用户排行 |
| GET  | `/v1/admin/usage` | — | LLM token 用量 |

### 3.3 `POST /v1/lite/search` 详解(插件主用)

**请求:**

```json
{
  "q": "FastAPI HMAC 签名失败",
  "top_k": 5,
  "scope": "auto",
  "task_type": "debugging"
}
```

`scope` 取值:
- `auto`(默认):personal + community 都搜
- `personal`:只看自己 owner / agent 的
- `community`:只看 acl=public 已发布的(社区池)

**响应:**

```json
{
  "results": [                                  // 合并结果, 已按 similarity 排
    {
      "experience_id": "...",
      "intent": "排查 FastAPI HMAC 签名失败",
      "query": "用户原话",
      "steps": ["...","..."],
      "outcome": "...",
      "task_type": "debugging",
      "acl": "private",                         // 或 "public"
      "ingest_path": "lite",
      "publish_status": "private",              // 或 "published"
      "similarity": 0.81,                       // 0-1 余弦相似度
      "source": "personal"                      // 或 "community"
    }
  ],
  "personal": [/* 同结构, 仅 personal 子集 */],
  "community": [/* 同结构, 仅 community 子集 */],
  "quota": {
    "owner": "user@sii.edu.cn",
    "publish_count": 3,
    "threshold": 3,
    "community_unlocked": true,
    "hint": "community pool unlocked"
  },
  "scope": "auto",
  "community_locked_hint": null                 // 没解锁时给个提示
}
```

### 3.4 `GET /v1/experiences/<eid>` 详解

**请求:**`GET /v1/experiences/abc123?include_trajectory=1`

**响应:**

```json
{
  "experience_id": "abc123-...",
  "agent_id": "...",
  "agent_name": "user-xxx",
  "task_type": "debugging",
  "source_model": "claude-code",
  "intent_text": "...",
  "query": "...",
  "outcome": "...",
  "script_steps": ["...","..."],
  "acl": "private",
  "sensitivity": "medium",
  "tags": [],
  "review_status": "auto_approved",
  "sanitization_status": "layer1_only",
  "publish_status": "private",
  "redactions": {"ipv4": 12, "email": 3},
  "strict_redactions": null,
  "created_at": "2026-05-05T13:30:00",
  "trajectory_score": null,
  "is_memory_eligible": 0,
  "turn_count": 18,                             // 添加新字段(自 0.6 版本)
  "session_id": "1e03ccdb-..."                  // 同上
  "trajectory": [                                // 仅当 ?include_trajectory=1
    {"role": "user", "content": "...", "ts": "..."},
    {"role": "assistant", "content": "...", "tool_calls": [...]},
    {"role": "tool", "content": "...", "tool_result_for": "..."}
  ]
}
```

trajectory turn 字段约定见 `docs/UPLOAD_LOGIC_AND_MANUAL.md` §3。每个
Anthropic content block 拆成独立 turn,`thinking` 块以
`💭 思考\n\n<plaintext>` 形式作为 assistant turn,`tool_use` 用
`tool_calls=[{id,name,input}]`,`tool_result` 用 `role=tool` + `tool_result_for`。

---

## 4. 路径 C:Python 直接 import

`exp_uploader.py` 是单文件 stdlib only,任何 Python 项目都可以:

```python
import sys, importlib.util
spec = importlib.util.spec_from_file_location(
    "exp_uploader",
    "/root/.experience-pool/bin/exp_uploader.py",
)
exp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(exp)

# 可用的 helper
cred = exp.load_credential()                   # 读 ~/.experience-pool/credentials/...
res  = exp.http_request(
    base_url="http://10.244.66.195:3080",
    method="POST",
    path="/v1/lite/search",
    body={"q": "...", "top_k": 5, "scope": "auto"},
    cred=cred,
)
print(res["results"])

# 客户端正则脱敏(在 push 前抹一遍)
clean_text, redaction_count = exp.sanitize("My API key is sk-ant-...")

# 给任何 dict / list 递归脱敏(tool_calls.input 之类)
clean_obj = exp.sanitize_node(my_dict, redaction_count)
```

---

## 5. 写一个最小 Claude Code skill

`~/.claude/skills/<your-skill>/SKILL.md`(自动加载):

```markdown
---
name: experience-lookup
description: 在写代码前先搜经验池, 看有没有同类问题已经被解决过。任何调试 / 集成 / 部署任务都先搜。
---

# experience-lookup

用户提了一个看上去做过的任务(调试已知工具, 配置 K8s, 部署服务等)时,
**第一件事**是搜经验池, 不是直接动手。

## 怎么搜

```bash
~/.experience-pool/bin/exp search --q "<一行任务描述>" --top-k 5 --json
```

返回 JSON。在 `results` 里挑 `similarity > 0.5` 的, 读它的 `steps` 和
`outcome`。如果命中,就在回复里说:

> "我搜到一条相关经验(`<eid8>`):'<intent>'。它的关键步骤是:1. ... 2. ...。
> 我打算复用这个方案,适配点是:..."

不要把原始 JSON 贴给用户。

## 拿单条详情

如果用户问 "那条经验完整内容是什么", 用:

```bash
~/.experience-pool/bin/exp get --eid <eid> --include-trajectory --json
```

气泡视图也可以引导用户开 portal `/me` 看。

## 如何上传你做的工作

每完成一个子任务, 在收尾回复尾部加一行:

    [task-summary]: <动词 + 对象, ≤80 char>

服务端 SessionEnd hook 会自动 push。**不需要插件主动调 push** —— hook 已经接管了。
```

把这个 SKILL.md 放到 `~/.claude/skills/experience-lookup/`,Claude Code 启动
时就会加载,任务匹配上就自动调。

---

## 6. 写一个最小 Cursor 插件

Cursor 全局 rules 在 `~/.cursor/rules/<name>.md`,语法跟 SKILL.md 等价:

```markdown
---
name: experience-lookup
description: ...
---
# experience-lookup
... (内容同上)
```

---

## 7. 错误码

| HTTP code | 何时 | 怎么修 |
|---|---|---|
| 401 `bad signature` | secret 跟 server 不匹配 / body 字节被 shell 二次转义 | 重新 bind / 检查 base url |
| 401 `not logged in` | 走 cookie 端点(`/v1/users/me`)但没 cookie | 浏览器先登 `/login` |
| 403 `agent does not own experience` | revoke / publish 用了别人的 eid | 只能操作自己 owner / agent 的 |
| 400 `trajectory is required` | 服务端 `EXP_REQUIRE_TRAJECTORY=1` | push body 带 trajectory 字段 |
| 404 `experience not found` | eid 写错 / 已 revoke | `exp list` 看看现有的 |
| 502 `bad gateway` | gateway → FastAPI 那段不通 | `curl -m 4 <base>/healthz` 看 |
| 5xx | server 异常 | `tail /tmp/exp-mvp/server.log` |

---

## 8. 关于 vscode notebook proxy URL

如果你的插件运行在「**不同于经验池 pod 的内网机器**」上,
**不能用** `http://10.244.66.195:3080`(那是 k8s pod IP, 跨 pod 不通)。
必须用 portal `/me` 上发的 proxy URL,大约长这样:

```
https://nat2-notebook-inspire.sii.edu.cn/.../proxy/3080
```

vscode-server proxy 会剥前缀转给 gateway,HMAC 签名仍按
`/v1/lite/search`(不含 proxy 前缀)算,工作正常。

---

## 9. 关于隐私与 ACL

- 所有 push 默认 `acl=private`,**只 owner 可见**。
- 想发布到 community(同公司其它人能搜到):
  1. `exp publish --eid <eid>` (会先跑 strict-public 审查,见 `docs/SANITIZATION.md` §4)
  2. 命中阻断的字段在 response.hits 里告诉你
  3. 一年内你的 owner 累计 publish ≥ 3 条才解锁 community 池(看 `exp quota`)
- session-extractor(批量回填)**硬编码 `acl=private`**,无法绕过。

详细脱敏规则:[`docs/SANITIZATION.md`](SANITIZATION.md)

---

## 10. 文档导航

| 想知道 | 看 |
|---|---|
| 全链路怎么从 Claude Code 落到数据库 | [`docs/HOWTO_CLAUDE_UPLOAD.md`](HOWTO_CLAUDE_UPLOAD.md) |
| LiteCard 字段 / API 契约 | [`docs/UPLOAD_LOGIC_AND_MANUAL.md`](UPLOAD_LOGIC_AND_MANUAL.md) |
| 四层脱敏每条规则 | [`docs/SANITIZATION.md`](SANITIZATION.md) |
| 给 LLM agent 看的行为契约 | [`agent-contract.md`](../agent-contract.md) |
| Skill 总览 | [`SKILL.md`](../SKILL.md) |
