# 脱敏管道说明

经验池上传链路里**四层脱敏**协作:客户端正则 → 服务端正则 → OPF 深度脱敏 →
LLM 商业敏感判定。这一篇讲清楚每一层做什么、漏什么、什么时候触发,以及上传前
你能控制的开关。

---

## 0. 总体顺序

```
原始 trajectory(本机)
  ↓ ① 客户端 L0 正则脱敏(常见 secret / PII,在本机抹一遍)
  ↓ HMAC 签名上传
服务端 /v1/lite/push
  ↓ ② 服务端 L1 正则脱敏(始终运行,对 trajectory + card + system + tools 全过)
  ↓ ③ 服务端 L2 OPF 深度脱敏(可远程调用,可 defer 到后台 worker)
  ↓ ④ 服务端 L3 LLM 商业敏感判定(仅当 L1 命中"高严重度"类别时触发)
  ↓ 写库 + 落 sidecar 文件 + 写向量
```

发布到 community 时,还会额外跑一层:

```
⑤ strict-public 严格审查(发布到 public 之前,任何命中 → 阻止发布并告诉用户改哪里)
```

---

## 1. 客户端 L0 + 服务端 L1(正则)

由 `core/exp_core/sanitize_rules.yaml` 驱动,**客户端和服务端跑同一份规则**。
覆盖范围按严重度分三档:

### 高严重度(`severity: high`)—— 命中即流到人工审核

这一组命中后,服务端会把 row 标成 `review_status='pending'`,需要 reviewer 人工
确认才能继续。**任何一条命中**都会触发。

| 类别 | 替换为 | 抓什么 |
|---|---|---|
| `pem_private_key` | `<PRIVATE_KEY>` | RSA / EC / DSA / OpenSSH / PGP `BEGIN PRIVATE KEY` 整段 |
| `ssh_pubkey` | `<SSH_KEY>` | `ssh-rsa` / `ssh-ed25519` / `ssh-dss` 公钥 |
| `gcp_sa_key` | `"private_key":"<PRIVATE_KEY>"` | GCP service account JSON 里的 private_key 字段 |
| `aws_access_key` | `<KEY>` | `AKIA...` / `ASIA...`(20 字符) |
| `jwt` | `<JWT>` | `eyJ...` 三段式 JWT |
| `bearer_token` | `Bearer <TOKEN>` | `Authorization: Bearer ...` 头里的 token |
| `anthropic_key` | `<SECRET>` | `sk-ant-xxxx...`(20+ 字符) |
| `openai_proj_key` | `<SECRET>` | `sk-proj-xxxx...` |
| `openai_key` | `<SECRET>` | `sk-xxxx...`(排除 `sk-ant-` / `sk-proj-`) |
| `xai_key` | `<SECRET>` | `xai-xxxx...`(40+ 字符) |
| `groq_key` | `<SECRET>` | `gsk_xxxx...`(40+ 字符) |
| `google_api_key` | `<SECRET>` | `AIza...`(35 字符) |
| `hf_token` | `<SECRET>` | `hf_xxxx...`(30+ 字符) |
| `mimo_token` | `<SECRET>` | `tp-xxxx...`(30+ 字符) |
| `stripe_secret` / `stripe_publishable` | `<SECRET>` | `sk_live_` / `sk_test_` / `pk_live_` / `pk_test_` |
| `github_token` | `<SECRET>` | `ghp_` / `gho_` / `ghu_` / `ghs_` / `ghr_`(20+) |
| `gitlab_token` | `<SECRET>` | `glpat-...`(20+) |
| `npm_token` / `vercel_token` / `supabase_token` / `cloudflare_token` / `sentry_dsn` | `<SECRET>` | 各家 token 前缀 |
| `slack_token` | `<SECRET>` | `xoxb-` / `xoxp-` / `xoxr-` / `xoxs-` / `xoxa-` |
| `generic_api_key` | `key=<SECRET>` | `api_key=...` / `password=...` / `secret_key=...` 等通用赋值形式 |
| `url_with_credentials` | `scheme://<USER>:<PASS>@host` | `https://user:pass@host`、`postgres://...`、`mongodb+srv://...` |
| `db_uri` | `scheme://<DB_URI>` | postgres / mysql / mongodb / redis / amqp / clickhouse 完整 URI |
| `idcard_cn` | `<ID_CARD>` | 中国 18 位身份证(含校验位) |
| `credit_card` | `<CARD>` | Luhn 算法验证的银行卡号(不是粗暴 16 位匹配) |

### 中严重度(`severity: medium`)—— 替换但不上人工审

| 类别 | 替换为 | 抓什么 |
|---|---|---|
| `email` | `<EMAIL>` | 邮箱 |
| `phone_intl` | `<PHONE>` | `+86 138 ...` 等国际形式(min 10 位数字) |
| `phone_cn` | `<PHONE>` | 中国手机号(`1[3-9]xxxxxxxxx`,前缀 `+86` 可选) |

### 低严重度(`severity: low`)—— 替换,只是在 `redactions` 里计数

| 类别 | 替换为 | 抓什么 |
|---|---|---|
| `ipv4` | `<IP>` | 标准 IPv4(含校验范围 0-255) |
| `ipv6` | `<IP>` | 标准 IPv6 |
| `home_path` | `<HOMEDIR>/` | `/Users/<name>/` 和 `/home/<name>/` 前缀(防机器指纹) |

### 配置项(可按团队改)

```yaml
internal_domains:
  - corp.example.com
  - sii.edu.cn
  - acme-internal.net

employee_id_prefixes:
  - EMP
  - STAFF
  - SJTU
```

`internal_domains` 命中时整个域名替换成 `<INTERNAL_HOST>`(右侧 suffix 匹配,
比如 `corp.example.com` 也会抹掉 `build.corp.example.com`)。
`employee_id_prefixes` 命中 `<PREFIX>\d{4,8}` 形态(如 `SJTU12345678`),
替换成 `<EMPLOYEE_ID>`。

### 怎么写到 trajectory 上

正则在三个地方都跑:

1. `turn.content`(role-level 文字)
2. `turn.tool_calls[].input`(**递归**进 dict / list,所有字符串叶子节点)
3. `turn.tool_result_for` 关联的输出(role=tool 那条 turn 的 content)

`card.query` / `intent` / `outcome` / `system` / `tools` / `meta` 也一并过。

---

## 2. 服务端 L2 OPF 深度脱敏

正则只能抓有结构的形状。**OPF**(OpenAI Privacy Filter)是一个上下文感知的
小模型,补正则漏掉的非结构化 PII —— 比如:

- 散在自由文本里的人名、地址、机构名
- "我老板叫王大力" 这种自然语言泄露
- 8 类标准 PII(person name / address / phone / email / DOB / SSN / org / med record)

OPF 部署在独立 GPU 机器(当前内网 `http://10.245.4.167:8085`),服务端按
`EXP_OPF_REMOTE_URL` 指过去。原因:OPF 模型 ~500 MB,跑在主 API 上会拖慢
push 响应。

### 三种运行模式

| 模式 | 触发方式 | 特点 |
|---|---|---|
| **同步**(production 不推荐) | `EXP_OPF_REMOTE_URL` 已配置 + `EXP_DEFER_OPF` 没开 | 每次 push 同步调 OPF,API 等返回。慢但 row 落地就 `done`。 |
| **延迟**(推荐) | `EXP_DEFER_OPF=1` | push 直接落 `sanitization_status='layer1_only'`,后台 worker 扫这种 row 补 OPF。**响应快,但需要 worker 在跑**。 |
| **关闭** | 没配 `EXP_OPF_REMOTE_URL` 也没装本地 opf 包 | 只跑 L1。`strict_redactions` 字段一直空。 |

### Backfill worker 启动方式

如果开了 `EXP_DEFER_OPF=1` 但没启动 worker,**所有 row 永远停在 `layer1_only`**。
启动:

```bash
EXP_OPF_REMOTE_URL=http://10.245.4.167:8085 \
EXP_DB_PATH=/tmp/exp-mvp/pool.db \
EXP_TRAJECTORIES_DIR=/tmp/exp-mvp/trajectories \
nohup python3 /inspire/hdd/.../experience-pool/scripts/exp_opf_worker.py \
  --interval 30 -v > /tmp/exp-mvp/opf-worker.log 2>&1 &
```

worker 是幂等的(扫的就是 `sanitization_status='layer1_only'` 的行),
重启 / 重跑都安全。建议加进 `babysit.sh` 一起看着。

### OPF 命中记录在哪

L2 命中的 8 类 PII 计数写到 `experiences.strict_redactions`(JSON 字段)。
L1 写的是 `experiences.redactions`,两个字段分开。

---

## 3. 服务端 L3 LLM 商业敏感判定

**只在 L1 命中"高严重度"类别时**触发。判定模型是 `EXP_LLM` 指向的后端
(默认尝试 `claude` CLI,内网当前是 `EXP_LLM=mock`,走 mock 占位)。

判定 prompt(`core/exp_core/sanitize.py:_LAYER3_SYSTEM`)让模型回答:

```json
{
  "is_sensitive": false,
  "categories": ["finance", "health", "internal-roadmap", ...],
  "rationale": "..."
}
```

如果 `is_sensitive=true`,row 标 `review_status='human_review'`,只有 reviewer
显式 approve 才能让其它 viewer(同 team / public)看到。

### 当前内网状态

`EXP_LLM=mock`,所以 L3 永远返回 "not sensitive"。**等价于关闭**。要让 L3 真跑,
切换到:

- `EXP_LLM=claude` —— 走本机 `claude` CLI(消耗用户订阅 quota)
- 或者把 `EXP_AUTO_LABEL_*` 配上,服务端走 OpenAI-compat endpoint

---

## 4. Strict-public(发布到 public 时的额外审查)

`core/exp_core/sanitize_public.py` 是独立一层,**只在 publish 时跑**,语义和
前面三层完全不同:

| 前三层 | strict-public |
|---|---|
| 替换成占位符,trace 还能用 | **检测就 reject**,告诉用户改哪里 |
| 在每次 push 都跑 | 只在用户点「发布到 community」时跑 |

抓的是会**指纹定位用户机器**的内容:

- `file:///Users/xxx/Library/...` 这种 file URI(暴露 OS 用户名 + 装了什么 app)
- `vscode-resource://`、`vscode-webview://` 等 IDE 内部协议(暴露稳定 resource UUID)
- `localhost` / `127.0.0.1` / `192.168.*` / `10.*` 等私有 IP(暴露内网拓扑)
- 未脱敏的绝对路径(`/inspire/hdd/.../username/...`)
- session UUID(可以反推用户其它私有 row)

任意一条命中 → publish 调用返回 4xx,body 里告诉用户:

```json
{
  "ok": false,
  "blocked_by": "strict_public",
  "hits": [
    {"name": "file_uri", "match": "file:///Users/xhh/...", "severity": "high"},
    ...
  ],
  "fix_hint": "remove the leading file:// URI before publishing"
}
```

用户清掉这些再点发布。

---

## 5. 客户端额外保护

`bin/exp_uploader.py` 里的 `sanitize()` / `sanitize_node()` 是**客户端**实现,
和服务端用同一份 `sanitize_rules.yaml` 但**早一步运行**。这是双保险:

- 即使服务端被攻陷或 bug 跳过 L1,raw secret **也不会离开本机**
- HMAC body 里看到的就是抹过的内容

`tool_calls[].input` 是 dict / list 嵌套结构,`sanitize_node()` **递归**走每个
字符串叶子,不会被 nesting 蒙混过去。

---

## 6. 状态字段速查

每条 row 在 `experiences` 表里有这些字段反映脱敏状态:

| 字段 | 取值 | 含义 |
|---|---|---|
| `sanitization_status` | `pending` | 还没跑(罕见,刚 INSERT 还没 commit) |
| | `layer1_only` | L1 跑过,L2 因 defer 还没跑 |
| | `done` | L1 + L2 都跑完了 |
| | `flagged` | L1 命中高严重度,L3 跑过判定 not sensitive |
| | `human_review` | L1 命中高严重度,L3 判定 is_sensitive,等人工 |
| `review_status` | `auto_approved` | 没命中 high,直接放行 |
| | `pending` | 命中 high,等 review |
| | `approved` | reviewer 显式批准 |
| | `rejected` | reviewer 显式驳回 |
| | `revoked` | 用户主动撤回 |
| `redactions` | `{"email": 5, "ipv4": 12, ...}` | L1 各类别命中次数 |
| `strict_redactions` | 同上,但是 L2 OPF 的 | OPF 没跑就为空 |
| `publish_status` | `private` / `published` / `unpublished` | 发布状态(独立于 ACL) |

---

## 7. 当前内网部署的实际状态(2026-05-05)

| 层 | 状态 |
|---|---|
| L0 客户端正则 | ✅ 工作,客户端必经 |
| L1 服务端正则 | ✅ 工作,所有 row 都过 |
| L2 OPF 同步调用 | ⚠️ `EXP_DEFER_OPF=1` 配着,所以**没在 push 路径上跑** |
| L2 OPF backfill worker | ❌ **没启动**,1491 行 row 全停在 `layer1_only`,`strict_redactions` 全空 |
| L3 LLM 商业敏感 | ⚠️ `EXP_LLM=mock`,实际不判定 |
| Strict-public publish 审查 | ✅ 实现了,但目前所有 row 都是 `private`,没人触发过 |

**如果你想完整跑通三层**,做这两件事:

1. 启 OPF backfill worker(看 §2 末尾命令)。10 分钟左右就能把所有 layer1_only 推到 done。
2. 切换 `EXP_LLM` 到 `claude` 或者把 `EXP_AUTO_LABEL_*` 配上,L3 才有效。

---

## 8. 自己想加规则怎么改

`core/exp_core/sanitize_rules.yaml` 加一项:

```yaml
- name: my_internal_token
  pattern: '\bMYORG_[A-Z0-9]{20,}\b'
  placeholder: <SECRET>
  severity: high
```

存盘,重启 server。**客户端那份是从 server 拉的同一份 yaml**,所以重启后下一次
push 就生效。建议同时加 `core/tests/test_sanitize.py` 的单测确认 pattern 不咬到
正常文字。

---

## 9. 排错

**row 永远 `layer1_only`**
OPF backfill worker 没在跑。看 §2。

**`strict_redactions` 永远空**
同上 —— L2 没跑。

**`review_status='pending'` 越来越多**
这是设计:命中高严重度的就要审。在 `/v1/admin/dashboard` 或 admin UI 上能看到
排队。reviewer approve / reject 之后状态会前进。

**正则误抓**(把不是 secret 的字符串当 secret 替换了)
看 `redactions` 字段哪一类计数大,对照 `sanitize_rules.yaml` 调那条 pattern。
建议先在 `core/tests/test_sanitize.py` 加测试用例复现,再调正则。

**publish 被 strict-public 阻止**
response 里有 `hits` 字段告诉你哪一行触发了哪条规则,直接按 `fix_hint` 改 trace
内容(或者 revoke 重传),就能 publish。
