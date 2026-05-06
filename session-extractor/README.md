# session-extractor

零依赖一次性回填工具:扫本机的 Claude Code / Codex / Hermes / openclaw 历史
session,批量上传到内网经验池的本人 **private** 库。模仿 `claude_sft_delivery`
的单入口模式。

## 隐私保证

`extract_and_upload.py` **代码里硬编码 `acl=private`**,没有 flag 能让上传变 public。
每条记录只有 `EXP_AGENT_NAME` 对应的本人能看,直到你在门户 `/me` 页显式发布。

## 怎么跑

门户 `/me` 页面有一行带好你 secret 的命令,大约这样:

```bash
curl -fsSL <EXP_BASE_URL>/session-extractor/run.sh | \
  EXP_AGENT_NAME='user-xxx' \
  EXP_AGENT_SECRET='<portal-issued-secret>' \
  EXP_BASE_URL='<EXP_BASE_URL>' \
  bash
```

这条命令做的事:

1. 从 `EXP_BASE_URL/session-extractor/extract_and_upload.py` 下载脚本
2. 自动检测 `~/.claude/projects/`、`~/.codex/sessions/` 等
3. **claude-code adapter 把 content block 拆开**:`text` / `thinking` / `tool_use` /
   `tool_result` 各自成一个独立的 turn,UI 才能分别气泡渲染
4. **codex adapter 同样支持** nested `{type, payload}` 格式,识别 `function_call` /
   `function_call_output` / `reasoning`,统一映射到 tool_calls / role=tool / 思考块
5. 客户端 L0 正则脱敏后,HMAC 签名 POST 到 `/v1/lite/push`,`acl=private`
6. 标题优先级:trajectory 中最后一条 `[task-summary]:` → 第一条真实用户消息的第一句
   (这个独立工具**不**调本地 LLM 总结——保持零依赖)
7. 服务端按 `(agent_id, fingerprint)` 去重,**重跑安全**

## 自己跑(已经下到本机的话)

```bash
EXP_AGENT_NAME='user-xxx' \
EXP_AGENT_SECRET='<hex>' \
EXP_BASE_URL='<EXP_BASE_URL>' \
python3 extract_and_upload.py [options]
```

## 常用 options(放在 `EXP_EXTRACTOR_FLAGS`)

| flag | 默认 | 作用 |
|---|---|---|
| `--sources <list>` | auto-detect | 逗号分隔:`claude-code,codex,hermes,openclaw` |
| `--limit N` | 不限 | 最多上传 N 条 |
| `--max-mb N` | 4 | 单 session 大小上限(MB),`0` 不限 |
| `--sleep S` | 0.5 | 两次上传之间停 S 秒,避免压垮服务端 |
| `--since <iso>` | 无 | 只跑这个日期之后的 |
| `--dry-run` | 关 | 列要传啥不动手 |
| `--verbose` / `-v` | 关 | 每条 session 打一行进度 |

例(全量、不限大小、不停顿):

```bash
EXP_EXTRACTOR_FLAGS='--max-mb 0 --sleep 0 --verbose'
```

## 输出形式

```text
[extractor] sources: claude-code, codex
[extractor] target:  <EXP_BASE_URL>  agent=user-xxx
[extractor] acl:     private (never public — by design)

[claude-code] found 21 session file(s)
  ✓ [1] claude-code/1e03ccdb → a3f8b21c  (acl=private)
  ⏎ [2] claude-code/030cf3d4 → b71d2e44 (already in pool)
  ✓ [3] claude-code/63999d18 → c4f0a512  (acl=private)
  ...

[extractor] DONE — uploaded=18  duplicate=2  skipped=1  failed=0
[extractor] visit your portal /me to review or revoke.
```

## 为什么独立成工具

- **零依赖**(stdlib only),Python ≥ 3.9 就能跑
- **不依赖 `exp` CLI** —— 适合刚拿到 secret、本机还没装东西的情况
- **幂等** —— 服务端按内容指纹去重,跑多少次都安全
- **`acl=private` 写死** —— 即使代码被改也只能私有(每次 `/run.sh` 重新下载,代码以
  服务端为准)

## 踩过的坑(看一眼避坑)

- 早期版本没有 `--no-session-persistence`,LLM 总结子进程会落盘成假 session 文件,
  被下次 daemon-tick 当真 session 抓走,标题就变成 `<transcript>`。这个版本已修。
- 服务端**不**自动跑 OPF backfill,需要单独启动 `exp_opf_worker.py`,否则你传上去的
  全是 `sanitization_status='layer1_only'`(只过了正则,深度脱敏 worker 没补)。
  详见 `docs/UPLOAD_LOGIC_AND_MANUAL.md` §6。
- 撤回(revoke)**不**清 `content_fingerprints`。撤回后想重传同一份内容,需要操作员
  手动 `DELETE FROM content_fingerprints WHERE experience_id='<eid>'`。
