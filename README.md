# db9 × Amazon Bedrock AgentCore Demo

一个跑在 **Amazon Bedrock AgentCore Runtime** 上的客服 Copilot（**Strands Agents + Bedrock Claude**），把 **[db9](https://db9.ai)** 当作 Agent 唯一的数据层：知识库、长期记忆、会话快照、业务数据、运行轨迹、产出报告都放在**同一个 db9 数据库**里——结构化数据进表，原始上下文进文件（fs9），两者都能用 SQL 查。

db9 调研报告（是什么、开源与否、与 Aurora / DynamoDB 的对比、竞品）：[docs/research.md](docs/research.md)

```
            ┌──────────── Amazon Bedrock AgentCore Runtime ────────────┐
 user ──►   │ agentcore_app.py (BedrockAgentCoreApp /invocations)      │
            │   └─ Strands Agent ── Bedrock Claude Sonnet 4.6          │
            │        tools: recall · search_knowledge · query_data     │
            │               remember · save_report                     │
            └───────────────┬──────────────────────────────────────────┘
                            │ PostgreSQL wire protocol (psycopg, TLS)
            ┌───────────────▼──────────────── db9 (one database) ──────┐
            │ tables : kb_chunks(vector) memories(vector) orders       │
            │          agent_runs                                      │
            │ fs9    : /kb/*.md  /memories/<user>/*.md                 │
            │          /sessions/<sid>/messages.json                   │
            │          /runs/<sid>/<run>.jsonl  /reports/<user>/*.md   │
            │ SQL    : embedding() · CHUNK_TEXT() · HNSW · fs9() · ... │
            └──────────────────────────────────────────────────────────┘
```

## 展示的 db9 能力

| # | 能力 | 在哪里 |
|---|---|---|
| 1 | **一条 SQL 完成 RAG 入库**：fs9 目录 → `CHUNK_TEXT` → `embedding()` → 向量表 | `scripts/01_bootstrap.py` |
| 2 | **表存记忆，文件存上下文**：`remember` 同时写 `memories` 表（向量）和 `/memories/...md` 文件；新会话通过 `recall` 找回 | `db9_agent/tools.py`、`scripts/02_agent_chat.py` |
| 3 | **会话快照放文件**：Strands 的 messages 存成 `/sessions/<sid>/messages.json`，跟着 AgentCore session id 走 | `db9_agent/agent.py` |
| 4 | **Agent 可观测性**：每次工具调用追加到 fs9 的 JSONL，再用 `SELECT … FROM extensions.fs9('/runs/*/*.jsonl')` 跨文件聚合 | `Tracer`、`02_agent_chat.py` |
| 5 | **分支即沙箱**：分支整个环境（表 + 向量 + 文件），在分支上做破坏性实验（重新切块、清空记忆），对比效果后删掉，生产库不受影响 | `scripts/03_branch_sandbox.py` |
| 6 | **一个租户 / 一个任务一个库**：秒级建真 Postgres，用完即删 | `scripts/04_db_per_tenant.py` |
| 7 | **自带 embedding（Bedrock Titan v2）**：向量在你自己的 AWS 账号里生成，db9 只负责存储和检索 | `scripts/05_bedrock_embeddings.py` |
| 8 | **AgentCore Runtime 契约**：`/ping` + `/invocations`，从 AgentCore 的 session header 取 session id，DSN 从 Secrets Manager 读取 | `agentcore_app.py`、`db9_agent/db.py` |

## 运行（本地）

前置：Python 3.12、[uv](https://docs.astral.sh/uv/)、有 Bedrock 访问权限的 AWS 凭证（默认 `us-west-2`，模型 `us.anthropic.claude-sonnet-4-6` 和 `amazon.titan-embed-text-v2:0`）。

```bash
# 1. db9 CLI + 数据库（匿名账号，无需注册）
curl -fsSL https://db9.ai/install | sh
db9 create --name agentcore-demo        # 记下打印的 connection string（只显示一次）

# 2. 环境
uv venv .venv --python 3.12 && uv pip install --python .venv/bin/python -r requirements.txt
cp .env.example .env                    # 填 DB9_DATABASE_URL（末尾加 ?sslmode=require）

# 3. 依次运行
.venv/bin/python scripts/01_bootstrap.py        # schema + 文件写入 fs9 + SQL 内 RAG 入库
.venv/bin/python scripts/02_agent_chat.py       # 两个会话的 Agent 对话 + 查看 Agent 留下的数据
.venv/bin/python scripts/03_branch_sandbox.py   # 分支沙箱（约 1 分钟，等待 CLONING）
.venv/bin/python scripts/04_db_per_tenant.py    # 每租户一个库
.venv/bin/python scripts/05_bedrock_embeddings.py

# 4. 以 AgentCore 的 HTTP 契约在本地起服务
.venv/bin/python agentcore_app.py &
curl -s localhost:8080/invocations -H 'Content-Type: application/json' \
  -H 'X-Amzn-Bedrock-AgentCore-Runtime-Session-Id: bob-session-0001-aaaaaaaaaaaaaaaaaaaaa' \
  -d '{"prompt":"我的睡袋订单什么时候发货？保修多久？","user_id":"u-bob"}'
```

## 部署到 AgentCore Runtime

```bash
# DSN 放进 Secrets Manager，不放进镜像或环境变量
aws secretsmanager create-secret --name db9/agentcore-demo --region us-west-2 \
  --secret-string "$(grep ^DB9_DATABASE_URL .env | cut -d= -f2-)"

.venv/bin/agentcore configure -e agentcore_app.py -r us-west-2 \
  --requirements-file requirements.txt --disable-memory --non-interactive
.venv/bin/agentcore launch \
  --env DB9_SECRET_ARN=<secret-arn> --env BEDROCK_MODEL_ID=us.anthropic.claude-sonnet-4-6
# 给 toolkit 生成的执行角色加 secretsmanager:GetSecretValue（只授权这一个 secret）
.venv/bin/agentcore invoke '{"prompt":"What is your return policy for used tents?","user_id":"u-alice"}'
```

AgentCore 容器需要能访问公网：db9 是公网端点（`pg.d0000.db9.io:5432`，TLS），目前没有 PrivateLink / VPC 私有接入。

## 实测结果（2026-10-09，us-west-2）

- SQL 内入库：4 个 md 文件 → 5 个 chunk → 向量化完成，约 2s
- Agent 一轮对话 9–14s（Claude Sonnet 4.6，3–4 次工具调用）；工具延迟：`query_data` 约 0.5s，`search_knowledge` 约 0.9s，`remember` 约 1.8s
- 新会话（空聊天历史）能通过 `recall` 拿回上一会话记住的「Nimbus Plus 会员」「西雅图自提柜」，并据此给出 60 天退货窗口
- 分支：请求 0.3s 返回，约 64s 变为 ACTIVE；fs9 的 14 个文件随分支一起复制；分支上把 chunk 改小后，检索距离从 0.54 降到 0.38，生产库不受影响
- 建库：CLI 端到端约 3.7–4s
- `embedding()` 与 Bedrock Titan v2 的向量一致（最大余弦距离 4e-7）

## 安全提示

- `.env` 已在 `.gitignore` 中。DSN 是 admin（superuser）凭证，因为 fs9 和 `embedding()` 都要求 superuser。生产中应把 SQL 工具和文件 / 向量工具拆成不同角色。
- `query_data` 用 `SET TRANSACTION READ ONLY`，并且只接受单条 SELECT/WITH，防止 LLM 写库；租户隔离由工具闭包绑定 `user_id`，不交给模型决定（`recall` 和 `remember` 强制按 user 过滤）。`query_data` 的 user 过滤目前只靠 prompt 约束，生产应加 RLS。
- 匿名 db9 账号的 bearer token 7 天过期（CLI 会自动刷新）；正式使用请 `db9 claim`。
