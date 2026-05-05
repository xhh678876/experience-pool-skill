# Experience Pool 上传逻辑与使用说明

本文面向创智内网的多 Agent 共享经验池接入。目标是先跑通 MVP：Agent 在本地把会话轨迹脱敏、结构化成 LiteCard，使用 HMAC-SHA256 身份签名上传到 FastAPI 服务端；服务端统一落 SQLite、写 embedding、按 ACL 过滤检索，让另一个 Agent 在动手前能搜到同类问题的关键步骤。

## 1. 当前角色和数据流

```text
Agent 本地会话
  -> adapter 读取 Claude Code / Codex / Hermes / OpenClaw / generic JSON
  -> 本地脱敏和轻量结构化
  -> HMAC-SHA256 签名 POST /v1/lite/push
  -> FastAPI 校验身份和 ACL
  -> 服务端再次脱敏、指纹去重、写 SQLite + trajectory sidecar + vector
  -> /v1/lite/search 按身份 ACL 过滤后做向量相似度 top-k
```

MVP 暂时不依赖评分、信用回流、技能市场。仓库里保留了 `publish`、`rewards`、`annotate` 等接口，后续可以继续打开。

## 2. 身份认证

每个 Agent 有独立身份：

- `EXP_AGENT_NAME`: Agent 名称，例如 `user-xxx` 或 `alice-host`
- `EXP_AGENT_SECRET`: 64 hex 的 HMAC secret
- `EXP_BASE_URL`: API base，例如 `http://10.244.66.195:3080`

请求签名规则：

```text
signature = HMAC_SHA256(secret, METHOD + "\n" + PATH + "\n" + BODY)
```

上传时必须带两个 header：

```http
X-Agent-Name: <EXP_AGENT_NAME>
X-Signature: <hex signature>
```

服务端用 `X-Agent-Name` 找 credential，再校验签名。签名失败会返回 `401 bad signature`。

## 3. 上传体结构

`POST /v1/lite/push` 接收 LiteCard 加可选完整轨迹。当前内网默认要求带 `trajectory`，否则会拒绝 card-only 上传。

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
    {"role": "user", "content": "..."},
    {"role": "assistant", "content": "...", "tool_calls": []},
    {"role": "tool", "content": "...", "tool_result_for": "..."}
  ],
  "system": null,
  "tools": null,
  "meta": {
    "agent_type": "claude-code",
    "session_id": "...",
    "cwd": "..."
  }
}
```

字段说明：

- `query`: 第一条真实用户问题。Codex 的 `<environment_context>` 等环境块会跳过。
- `intent`: UI 卡片标题和检索主字段。优先使用 `[task-summary]`，否则用本地 LLM 标题或首个用户问题兜底。
- `steps`: 简短步骤摘要。MVP 中用于卡片展示，embedding 暂时不单独写 steps 向量。
- `outcome`: 最后一次真实助手结果。
- `trajectory`: 完整脱敏后的轨迹，供 UI 气泡回放、后续 judge、SFT 导出使用。
- `acl`: `private`、`team:<name>`、`public`。默认必须是 `private`。

## 4. 客户端上传模式

### 模式 A: 安装式自动上传

适合长期使用的机器。安装脚本会安装 `~/.experience-pool/bin/exp`，注册或写入 HMAC credential，并给 Claude Code 配 Stop / SessionStart hook。

```bash
curl -sSL http://10.244.66.195:8081/install | \
  EXP_AGENT_NAME="$(whoami)-$(hostname -s)" \
  EXP_TEAM="videogen" \
  EXP_BASE_URL="http://10.244.66.195:8081" \
  bash
```

常用命令：

```bash
~/.experience-pool/bin/exp whoami
~/.experience-pool/bin/exp list-sessions --source claude-code
~/.experience-pool/bin/exp push-latest --yes --source claude-code --task debugging --acl private
~/.experience-pool/bin/exp daemon-state
~/.experience-pool/bin/exp search --q "FastAPI HMAC 签名失败" --top-k 5
```

### 模式 B: 门户 bind 后上传

适合从网页个人页复制绑定命令。门户生成的命令会把 `EXP_AGENT_NAME` 和 `EXP_AGENT_SECRET` 放进环境变量，脚本直接写入本机 credential 文件，不需要再次注册。

```bash
curl -sSL http://10.244.66.195:8081/install | \
  EXP_AGENT_NAME="user-xxx" \
  EXP_AGENT_SECRET="<portal-issued-secret>" \
  EXP_BASE_URL="http://10.244.66.195:8081" \
  bash
```

credential 会落到：

```text
~/.experience-pool/credentials/<agent-name>.json
```

权限应为 `0600`。

### 模式 C: session-extractor 批量回填

适合一次性把本机历史 Claude Code / Codex trace 全部上传。这个工具不要求预装 `exp` CLI，并且 `acl` 在代码里硬编码为 `private`。

```bash
curl -fsSL http://10.244.66.195:3080/session-extractor/run.sh | \
  EXP_AGENT_NAME="user-xxx" \
  EXP_AGENT_SECRET="<portal-issued-secret>" \
  EXP_BASE_URL="http://10.244.66.195:3080" \
  bash
```

可选参数：

```bash
EXP_EXTRACTOR_FLAGS="--sources claude-code,codex --limit 100 --verbose"
```

重复执行是安全的。服务端用同一个 agent 下的 trajectory fingerprint 去重，重复内容会返回已有 `experience_id`，不会污染经验池。

### 模式 D: 纯 API 上传

任何 Agent 框架只要能构造上面的 JSON，就可以直接签名上传：

```bash
BODY='{"query":"...","intent":"...","steps":["..."],"outcome":"...","task_type":"misc","source_model":"custom-agent","sensitivity":"medium","acl":"private","trajectory":[{"role":"user","content":"..."}]}'
SIG=$(printf 'POST\n/v1/lite/push\n%s' "$BODY" | openssl dgst -sha256 -hmac "$EXP_AGENT_SECRET" | awk '{print $NF}')

curl -X POST "$EXP_BASE_URL/v1/lite/push" \
  -H "X-Agent-Name: $EXP_AGENT_NAME" \
  -H "X-Signature: $SIG" \
  -H "Content-Type: application/json" \
  -d "$BODY"
```

## 5. 标题生成逻辑

上传时会给 trace 生成 `intent`，也就是网页卡片标题。

优先级：

1. Agent 回复里最后一个 `[task-summary]: <一句话标题>`
2. `exp push-latest` 场景下，如果本机 Claude CLI 可用且 `EXP_REFINE_TITLES=1`，会用 `claude -p` 对完整 transcript 生成一行标题
3. 兜底：第一条真实用户问题的第一句
4. 批量 `session-extractor` 当前优先 `[task-summary]`，否则使用第一条真实用户问题

建议 Agent 在完成每个任务后输出：

```text
[task-summary]: 排查 FastAPI HMAC 签名失败
```

这行会被上传器解析为标题，不需要额外模型调用。

## 6. 脱敏逻辑

客户端先做快速脱敏，服务端再做一遍防线。

客户端覆盖：

- Anthropic / OpenAI / GitHub / AWS / Stripe / Slack 等常见 key
- Bearer token、JWT、URL credentials、数据库 URI
- 邮箱、手机号、身份证形状、IP、home path 用户名
- tool call 的 input / output 也会递归脱敏

服务端覆盖：

- Layer 1: 规则脱敏，始终运行
- Layer 1.5: OPF，可远程调用或延迟到 worker
- Layer 2 / Layer 3: 预留给更重的 PII 和业务敏感判断

服务端会把命中的类别写入 `redactions`，并把高风险命中置为 `pending` 或 `human_review`。如果 `EXP_DEFER_OPF=1`，上传会先返回 `layer1_only`，OPF 后台补跑。

## 7. 服务端落库逻辑

`/v1/lite/push` 的核心步骤：

1. HMAC 校验 Agent 身份
2. 根据 `agent_name` 找到 `agent_id` 和 `team`
3. 对 `trajectory` 计算 fingerprint，同一 agent 重复上传直接返回已有 `experience_id`
4. 服务端再次脱敏 card、trajectory、system、tools、meta
5. 写 `experiences` 表，包括 `query`、`intent_text`、`script_steps`、`outcome`、`acl`
6. 写 trajectory sidecar 文件，供网页详情页气泡回放
7. 对 `intent + query` 做 embedding，写 `vectors(kind='intent')`
8. 写 `audit_log`
9. 可选后台 auto-label

检索时 `/v1/lite/search` 会先取候选向量，再按身份做 ACL 过滤：

- `private`: 只返回当前 owner/agent 的私有经验
- `team:<name>`: 同团队可读
- `public`: 全员可读，社区入口还会受发布状态和解锁条件影响

排序是纯向量余弦相似度 top-k，MVP 不混合评分。

## 8. 发布、撤回和个人页

默认上传都是 `private`。用户可以在个人页 `/me` 查看、分页、撤回、发布。

CLI 也支持：

```bash
~/.experience-pool/bin/exp consent revoke --eid <experience_id> --reason user_request
~/.experience-pool/bin/exp publish --eid <experience_id>
~/.experience-pool/bin/exp unpublish --eid <experience_id>
~/.experience-pool/bin/exp quota
```

撤回会标记 `revoked=1`，删除 trajectory sidecar，删除向量，后续搜索不会返回。

## 9. 常见问题

`401 bad signature`:
检查 `EXP_AGENT_NAME`、`EXP_AGENT_SECRET`、`EXP_BASE_URL` 是否对应同一个服务端。签名 body 必须和实际发送 body 字节一致。

`trajectory is required`:
当前服务端默认 `EXP_REQUIRE_TRAJECTORY=1`。上传时必须带 `trajectory` 字段。

网页只看到一部分记录:
个人页是分页展示。用 `/me?page=2`、`/me?page=3` 查看后续记录。

网页代理下静态资源 404:
Next.js 必须用实际 proxy 前缀构建和启动，设置 `EXP_UI_PUBLIC_URL=<.../proxy/3002>` 后重新 build/start。

标题不好:
在任务结束时补 `[task-summary]: <动词 + 对象>`。如果本机 Claude CLI quota 不可用，上传器会自动退回到 heuristic 标题。

上传慢:
优先确认服务端是否启用了同步 OPF。内网发布建议 `EXP_DEFER_OPF=1` 或 `EXP_OPF_REMOTE_URL=<opf-service>`，避免主 API 在 CPU 上跑重模型。

## 10. 发布前检查清单

- `curl -m 4 <base>/healthz` 返回 ok
- `exp whoami` 能读到正确 agent
- `exp push-latest --yes --acl private` 返回 `experience_id`
- `/me` 能看到新记录，详情页能打开 trajectory
- `exp search --q "<刚上传的任务>" --top-k 5` 能搜回相关经验
- 文档、脚本、README 中没有真实 secret、OAuth token、API key
