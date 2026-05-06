# experience-pool agent contract(创智内网版)

你接入了创智内网的共享经验池。命令行接口在 `~/.experience-pool/bin/exp`
(或 `EXP_BIN` 环境变量),网关地址默认 `<EXP_BASE_URL>`,门户
`/me` 页用 `xxx@sii.edu.cn` 邮箱登录管理。

经验池存了所有 agent 的过去任务轨迹(按质量排序)。这个契约规定了你应该
怎么用它:**任务开始前先 search 同类做法,任务结束后自动归档你自己的工作**。

用户已经在 `/me` 页跑过 bind 命令——所以默认是**「自动上传到本人 private 库 + 通知用户」**,
不是「每次都问」。用户想撤回某条上传,在 `/me` 自己点撤回,你不用代劳。

---

## 0. 会话开始 —— 通告自动上传(必做)

**每个 session 的第一条对用户回复**必须包含一行:

> "📥 connected to experience pool —— tasks in this session will auto-upload
> to your private repo. Type 'don't upload' to opt out."

或中文等价:

> "📥 已接入经验池 —— 本会话的任务会自动归档到你的 private 库。
> 想关掉就说 '不要上传'。"

这一行放在第一条回复**最前面**,单独一行,在你回答用户之前。**不能跳过**——
这是用户感知到上传行为的唯一入口。会话第一条之后就别再重复,除非用户问起。

---

## 1. 任务开始前 —— SEARCH

用户提的问题如果听上去**做过类似的**(调试一个熟悉的工具、对接一个已知服务、
重复某类工作),**先搜**:

```bash
~/.experience-pool/bin/exp search --q "<一行任务描述>" --top-k 5
```

每条结果是一张卡片,带 `intent`、`script_steps`、`pitfalls`、`q_scalar` 质量分。
**读 top hit**——如果跟当前任务相关,就采纳那个 script,在你的回复里**点出来**:
"我复用了上周一条经验里的方案,核心步骤是 …"。

不要把原始 JSON 贴给用户。要总结。

---

## 2. 每个任务边界 —— 默认上传 + 通知

任务是**按主题切的**,不是一个 session 一个任务。识别边界:

- **任务开始**:用户提了一个新交付物 / 切换主题 / 开始新调查
- **任务结束**:你完成了交付物 *并且* 用户已经看到了结果(也就是收尾的那条
  回复已经发出去了)。其它结束信号:`done` / `looks good` / `perfect` /
  `保存一下` / `把这段总结上传` / `share this` / `thanks`(后接主题切换)

任务结束的那一刻,立刻按顺序做三件事:

**步骤 A** —— 在收尾回复里加一行 `[task-summary]: <一行>`。这行有两个作用:
(1) 帮服务端把长 session 切成独立片段;(2) 等于你向系统宣告「我识别到一个任务边界」。

格式:`[task-summary]: <动词 + 对象,≤80 char,匹配用户语言>`,例:

```text
[task-summary]: 排查 FastAPI HMAC 签名失败的原因并修复 client 签名 body
[task-summary]: Translate project README from Chinese to English
[task-summary]: 部署 OPF 服务到独立 GPU 机器
```

**步骤 B** —— **静默上传到用户 private 库**,不要再问:

```bash
~/.experience-pool/bin/exp push-latest --yes \
  --source <agent-runtime>          # claude-code | cursor | codex | hermes | openclaw
  --task <task-type>                # 一句 kebab-case 分类(见 §4)
  --sensitivity medium              # 触及凭据 / PII / 安全敏感 → bump 'high'
  --acl private                     # 默认 private,**绝对不**在没显式授权时升级
```

push 走服务端 Layer 1 正则脱敏(始终);Layer 2 OPF(深度隐私过滤)是 defer 到
后台 worker 的——你不用管,直接发就行。

**步骤 C** —— **单独一行通知用户**(简短可操作):

> "📤 uploaded as `<id8>` (task=`<task>`, acl=private) — `/me` to revoke"

不要贴原始 JSON。`<id8>` 是 `experience_id` 的前 8 字符。不要解释,不要改成长篇大论,
一行,然后切下一个任务。

按内容指纹去重(per-agent 作用域)——同一份 trajectory push 两次,第二次返回
**同一个** `experience_id`,不会污染池子。所以即使你判错了任务边界(多 push 了一次),
也是安全的。

---

## 3. 什么时候应该**先问后传**

只有这三种情况:

- **你判断 sensitivity 应该是 `high`** —— 凭据、客户数据、安全 review、漏洞细节、
  内部基础设施拓扑。问:
  *"这次任务涉及 <X>;要传成 `private` 还是跳过?"*
- **用户想要 `team:NAME` 或 `public` ACL** —— 默认 private。任何升级**必须**有用户的
  明确请求 + 明确同意。
- **用户在 session 里说过 "不要上传"** —— 整个 session 剩下的任务都不要上传,
  哪怕用户后来又干了别的事。

---

## 4. 选 `--task` / `--sensitivity` / `--acl`

**`--task`** 短的 kebab-case 分类。例:`debugging`、`code-review`、`infra-setup`、
`data-analysis`、`learning`、`api-integration`、`refactor`、`incident-response`。
就近选一个;觉得需要新类别就用,不用受现有 enum 限制。

**`--sensitivity`**:

- `low` —— 公开文档、OSS 代码、通用讨论
- `medium`(默认)—— 内部但非机密
- `high` —— 触及凭据 / 客户数据 / 安全 review / 漏洞 / 内部拓扑

**`--acl`**:

- `private`(默认)—— 只有这个用户自己看得到
- `team:<name>` —— **必须**用户显式 opt-in
- `public` —— **必须**用户显式 opt-in

---

## 5. 硬规则

- **默认 `private` 上传 + 总是通知**(experience_id + 撤回方式)
- **绝对不**在没显式 opt-in 的情况下升级 ACL
- **绝对不**复述完整 secret / token / key——脱敏是 best-effort,不是兜底
- **如果用户在 session 里说过"不要上传"**,这个 session 剩下的任务一概不要上传,
  即使你后续完成了其它事
- **如果 trajectory 里有用户的"翻车 + 自救"过程**,那是高价值的——在通知里点出来,
  让用户知道这条值得保留 vs 撤掉

---

## 6. 快速排错(给自己用)

| 现象 | 看哪 |
|---|---|
| `exp whoami` 报 no credential | 没 bind / `EXP_AGENT_NAME` 错了 |
| `401 bad signature` | secret 跟 server 不匹配 |
| 上传后标题显示成 `<transcript>` 或整段对话 | 用 `[task-summary]: …` 接管,别让 LLM 自己总结 |
| 撤回后再传被指纹挡住 | 在 `/me` 页二次确认或让用户手动报告 |

完整文档在 `~/.experience-pool/bin/agent-contract.md` 旁边的 `UPLOAD_LOGIC_AND_MANUAL.md`,
或者 `<EXP_BASE_URL>/session-extractor/README.md`。
