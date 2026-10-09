# db9 × Amazon Bedrock AgentCore Demo

一个做成 **多店 SaaS** 的客服 Copilot：同一套 Agent 代码（**Strands Agents + Bedrock Claude**，跑在 **Amazon Bedrock AgentCore Runtime** 上）服务很多家店，每家店是一个租户，拥有**自己独立的 [db9](https://db9.ai) 数据库**——知识库、订单、顾客记忆、会话快照、运行轨迹、报告全在里面。结构化数据进表，原始上下文进文件（fs9），两者都能用 SQL 查。

另外附一个 db9 最有优势、Aurora / DynamoDB 很难做到的场景：**每个分析任务一个一次性数据库**（`scripts/06_analyst_sandbox.py`）。

db9 调研报告（是什么、开源与否、与 Aurora / DynamoDB 的对比、竞品）：[docs/research.md](docs/research.md)

```
                    ┌────────── Amazon Bedrock AgentCore Runtime ──────────┐
 shopper of store X │ agentcore_app.py  (/invocations, store_id -> tenant) │
 ─────────────────► │   └─ Strands Agent ── Bedrock Claude Sonnet 4.6      │
                    │        tools: recall · search_knowledge · query_data │
                    │               remember · save_report                 │
                    └──────┬─────────────────┬─────────────────┬───────────┘
          tenants.resolve  │                 │                 │   (DSN per store: Secrets Manager)
                 ┌─────────▼──────┐ ┌────────▼───────┐ ┌───────▼────────┐
                 │ db9: Nimbus    │ │ db9: Peak      │ │ db9: Tidepool  │  one database per store
                 │ Gear           │ │ Cycles         │ │ Surf Co.       │
                 │ tables: kb_chunks(vector) memories(vector) orders agent_runs     │
                 │ fs9   : /config/store.json /kb/*.md /memories/ /sessions/ /runs/ │
                 └────────────────┘ └────────────────┘ └────────────────┘
```

## 场景：客服 Copilot 卖给很多家店

| 店（租户） | 卖什么 | 退货政策（各自知识库里） |
|---|---|---|
| Nimbus Gear | 户外装备 | 30 天；用过的帐篷可退店铺积分，扣 15%；会员 60 天 |
| Peak Cycles | 自行车 | 14 天且必须没骑过；骑过的整车不退，可免费换一次尺码 |
| Tidepool Surf Co. | 冲浪板 / 潜水衣 | 冲浪板 7 天且未打蜡；潜水衣 45 天可换，穿过一次也行 |

`scripts/04_saas_multi_store.py` 实测（同一个顾客 `u-alice` 在三家店都买过东西，问同一句话「用过一次了还能退吗？我都有哪些订单？」）：

- **入驻**：新店 = `db9 create` + 一次 provision（写入品牌配置、KB 文件、在 SQL 里切块 + 向量化、导入订单）。端到端约 17–22 秒（建库 5–7 秒，provision 11–15 秒）。
- **服务**：同一份 Agent 代码给出三个完全不同的答案，各自引用自己店的 `/kb/returns-policy.md` 和自己店的订单（NG-*/PC-*/TS-*）；Nimbus Gear 还从**自己店**的记忆里认出她是会员，另外两家店里她没有任何记忆。
- **隔离**：每家店的表、文件、记忆物理上在不同数据库，凭证也不同——用 Peak Cycles 的密码登录 Nimbus Gear 的库直接 `Password authentication failed`。
- **退订**：Tidepool 退订 = 删一个库，表、向量、文件、记忆、轨迹全部消失；之后它的请求直接被拒（fail closed，不会落到别的店的库）。

路由规则在 `db9_agent/tenants.py`：`store_id -> DSN`，本地用 `.tenants.json`（gitignored），AgentCore 上用 Secrets Manager 的 `db9/tenants/<store_id>`。Agent 的工具闭包同时绑定了「哪家店的库」和「哪个顾客」，LLM 无法自己选择访问哪个租户。

### 同一个 SaaS，AWS 原生方案 vs db9

| 需求 | AWS 原生方案（常见做法） | db9 的做法 |
|---|---|---|
| 租户隔离模型 | Pool：多店共用一个 Aurora 集群，用 schema 或 RLS 隔离；Silo（一店一集群）一般只给大客户 | Silo：一店一库，默认就是物理隔离 |
| 新店入驻 | 建 schema / 加 RLS 策略 + S3 前缀 + IAM 策略 + Bedrock KB 数据源或 metadata 过滤 + AgentCore Memory namespace；一店一集群要几分钟 | `db9 create` + 一条入库 SQL，实测约 20 秒 |
| 订单等结构化数据 | Aurora PostgreSQL | 店自己库里的表 |
| 知识库检索 | Aurora pgvector，或 Bedrock Knowledge Bases（S3 Vectors / OpenSearch） | 店自己库里的向量 + `embedding()` |
| 长期记忆 | AgentCore Memory（按 namespace 区分店和顾客） | 店自己库里的 `memories` 表 + 文件 |
| 原始对话、轨迹、报告 | S3（按店分前缀） | 店自己库里的 fs9 |
| 会话状态 | DynamoDB | 店自己库里的 `/sessions/*.json` |
| 退订 / 删除数据（GDPR） | 分别清理 Aurora schema、S3 前缀、KB 数据源、Memory namespace，容易漏 | 删一个库 |
| 一家店的实验环境 | Aurora Fast Clone 只复制库，S3 / KB 要另外处理 | `db9 branch` 连文件一起复制（全量，约 60 秒） |
| 生产保障 | Multi-AZ、VPC / PrivateLink、IAM、多区域、SLA、合规认证 | 公网端点、仅 us-west-2、无公开 SLA、闭源 |

结论：db9 让「一店一库」这种最简单、隔离最强的模型变得便宜且秒级，而且一个库就装下了 Agent 的全部数据，入驻和退订都是一次调用。AWS 原生方案组件更多，但生产保障强得多。对隔离要求高、店铺数量多、每家数据量小的 SaaS，db9 的形态更顺手；对需要 VPC、合规和 SLA 的客户，AWS 原生方案是更稳的选择。

## 最适合 db9 的场景：一次性分析沙箱（Aurora / DynamoDB 都不合适）

`scripts/06_analyst_sandbox.py`：店主把一堆**原始导出文件**（订单 CSV、退货 CSV、物流事件 JSONL、评论 JSONL、一封供应商邮件）扔给平台，问「9 月退货率为什么涨了这么多？」。针对这**一个问题**：

1. 平台新建一个 db9 库，文件原样放进 fs9——不定义 schema，不写 ETL。
2. Agent 在这个库里拥有**完整权限**：自己 `CREATE TABLE AS SELECT ... FROM extensions.fs9('/uploads/x.csv')`，自己 join、聚合、读邮件，最后写 `/out/report.md`。破坏半径只有这个一次性库。
3. 拿到报告，删库。

实测（Claude Sonnet 4.6，约 3.5 分钟，29 次工具调用，4 条 SQL 报错后 Agent 自己改正）：Agent 自己把 4 个文件物化成表，找出主因是 9 月 8 日起发货的 **B-0908 批次背包腰带扣缺陷**（该批次退货率 39.4%，旧批次 8.3%；**剔除该批次后 9 月退货率 4.99%，与 8 月 5.06% 持平**），并把供应商邮件、差评原文作为证据；次因是 QuickShip 9 月配送时效从 2.9 天拉长到 4.7 天。这两个结论正是数据生成器里埋下的答案。

为什么这件事 Aurora / DynamoDB 做不好：

| 这个场景需要 | db9 | Aurora PostgreSQL | DynamoDB |
|---|---|---|---|
| 每个任务一个全新的完整数据库 | 一次 API 调用，秒级；用完即删 | 新建集群要几分钟，按实例计费；集群数有区域配额 | 建表快，但没有 SQL |
| 不建表、不写 ETL，直接用 SQL 查 CSV / JSONL | `extensions.fs9('/uploads/*.csv')` | 不支持 `file_fdw`；`aws_s3` 导入需要先建好列定义正确的表，也就是在看到文件之前就写好管道 | 不支持 |
| 给 LLM 完整 DDL 权限也安全 | 库是一次性的，破坏半径就是它自己 | 共享集群上给 LLM DDL 是安全问题；一任务一集群又太慢太贵 | — |
| 表、文件、向量、报告在同一个可丢弃单元 | 是，删库即全部清理 | 文件要放 S3，向量要 pgvector，清理要分别做 | 否 |
| join、聚合、窗口函数 | 有 | 有 | 没有 |

需要说清楚的是：Aurora 技术上能拼出来（预置一个池子、S3 + Lambda 做导入、每任务一个 schema），但「每个任务秒级拿到一个完整可丢弃的库 + 零 ETL 查原始文件 + 放心给 Agent 全部权限」这三点同时满足，是 db9 的形态天然提供、AWS 现有托管数据库没有直接对应的。同类产品里 Neon / Xata（CoW 分支）、Turso + AgentFS 也在做这件事。

实测中观察到的 db9 局限：一条把 2,146 行 JSONL 物化成表的 `CREATE TABLE AS` 超过 60 秒超时（重试成功），几条多表 join 返回 `internal error`（Agent 改写后成功）。也就是说兼容性和性能还不稳，适合沙箱，不适合关键链路。

## 展示的 db9 能力

| # | 能力 | 在哪里 |
|---|---|---|
| 1 | **一条 SQL 完成 RAG 入库**：fs9 目录 → `CHUNK_TEXT` → `embedding()` → 向量表 | `db9_agent/db.py`（`INGEST_SQL`）、`scripts/01_bootstrap.py` |
| 2 | **表存记忆，文件存上下文**：`remember` 同时写 `memories` 表（向量）和 `/memories/...md` 文件；新会话通过 `recall` 找回 | `db9_agent/tools.py`、`scripts/02_agent_chat.py` |
| 3 | **会话快照放文件**：Strands 的 messages 存成 `/sessions/<sid>/messages.json`，跟着 AgentCore session id 走 | `db9_agent/agent.py` |
| 4 | **Agent 可观测性**：每次工具调用追加到 fs9 的 JSONL，再用 `SELECT … FROM extensions.fs9('/runs/*/*.jsonl')` 跨文件聚合 | `Tracer`、`02_agent_chat.py` |
| 5 | **分支即沙箱**：分支整个环境（表 + 向量 + 文件），在分支上做破坏性实验，对比后删掉，生产库不受影响 | `scripts/03_branch_sandbox.py` |
| 6 | **多店 SaaS，一店一库**：入驻、按店路由、隔离、退订 | `db9_agent/tenants.py`、`scripts/04_saas_multi_store.py` |
| 7 | **自带 embedding（Bedrock Titan v2）**：向量在你自己的 AWS 账号里生成，db9 只负责存储和检索 | `scripts/05_bedrock_embeddings.py` |
| 8 | **一次性分析沙箱**：每个问题一个库，SQL 直接查原始文件，Agent 拥有完整权限 | `scripts/06_analyst_sandbox.py` |
| 9 | **AgentCore Runtime 契约**：`/ping` + `/invocations`，从 AgentCore 的 session header 取 session id，DSN 从 Secrets Manager 读取 | `agentcore_app.py`、`db9_agent/db.py` |

## 运行（本地）

前置：Python 3.12、[uv](https://docs.astral.sh/uv/)、有 Bedrock 访问权限的 AWS 凭证（默认 `us-west-2`，模型 `us.anthropic.claude-sonnet-4-6` 和 `amazon.titan-embed-text-v2:0`）。

```bash
# 1. db9 CLI + 默认数据库（第一家店 Nimbus Gear 用它；匿名账号无需注册，最多 5 个库）
curl -fsSL https://db9.ai/install | sh
db9 create --name agentcore-demo --show-password   # 记下 connection string 和密码

# 2. 环境
uv venv .venv --python 3.12 && uv pip install --python .venv/bin/python -r requirements.txt
cp .env.example .env                    # 填 DB9_DATABASE_URL（末尾加 ?sslmode=require）

# 3. 依次运行
.venv/bin/python scripts/01_bootstrap.py        # 第一家店：schema + 文件写入 fs9 + SQL 内 RAG 入库
.venv/bin/python scripts/02_agent_chat.py       # 两个会话的 Agent 对话 + 查看 Agent 留下的数据
.venv/bin/python scripts/03_branch_sandbox.py   # 分支沙箱（约 1 分钟，等待 CLONING）
.venv/bin/python scripts/04_saas_multi_store.py # 多店 SaaS：入驻 2 家新店、按店服务、隔离、退订（--keep 保留 Peak Cycles）
.venv/bin/python scripts/05_bedrock_embeddings.py
.venv/bin/python scripts/06_analyst_sandbox.py  # 一次性分析沙箱（约 3–4 分钟；--keep 保留沙箱库）

# 4. 以 AgentCore 的 HTTP 契约在本地起服务
.venv/bin/python agentcore_app.py &
curl -s localhost:8080/invocations -H 'Content-Type: application/json' \
  -H 'X-Amzn-Bedrock-AgentCore-Runtime-Session-Id: bob-session-0001-aaaaaaaaaaaaaaaaaaaaa' \
  -d '{"prompt":"我的睡袋订单什么时候发货？保修多久？","user_id":"u-bob","store_id":"nimbus-gear"}'
```

## 部署到 AgentCore Runtime

```bash
# 默认店的 DSN 放进 Secrets Manager，不放进镜像或环境变量
aws secretsmanager create-secret --name db9/agentcore-demo --region us-west-2 \
  --secret-string "$(grep ^DB9_DATABASE_URL .env | cut -d= -f2-)"
# 其他店：每店一个 secret，名字 = 前缀 + store_id
aws secretsmanager create-secret --name db9/tenants/peak-cycles --region us-west-2 \
  --secret-string "$(jq -r '."peak-cycles".dsn' .tenants.json)"

.venv/bin/agentcore configure -e agentcore_app.py -r us-west-2 \
  --requirements-file requirements.txt --disable-memory --non-interactive
.venv/bin/agentcore launch --env DB9_SECRET_ARN=<secret-arn> \
  --env DB9_TENANT_SECRET_PREFIX=db9/tenants/ --env BEDROCK_MODEL_ID=us.anthropic.claude-sonnet-4-6
# 给 toolkit 生成的执行角色加 secretsmanager:GetSecretValue（只授权 db9/agentcore-demo* 和 db9/tenants/*）
.venv/bin/agentcore invoke '{"prompt":"Can I return a bike I rode once?","user_id":"u-alice","store_id":"peak-cycles"}'
```

AgentCore 容器需要能访问公网：db9 是公网端点（`pg.d0000.db9.io:5432`，TLS），目前没有 PrivateLink / VPC 私有接入。

## 实测结果（2026-10-09，us-west-2）

- SQL 内入库：4 个 md 文件 → 5 个 chunk → 向量化完成，约 2s
- Agent 一轮对话 9–16s（Claude Sonnet 4.6，3–4 次工具调用）；工具延迟：`query_data` 约 0.5s，`search_knowledge` 约 0.9s，`remember` 约 1.8s
- 新会话（空聊天历史）能通过 `recall` 拿回上一会话记住的「Nimbus Plus 会员」「西雅图自提柜」，并据此给出 60 天退货窗口
- 分支：请求 0.3s 返回，约 64s 变为 ACTIVE；fs9 的 14 个文件随分支一起复制；分支上把 chunk 改小后，检索距离从 0.54 降到 0.38，生产库不受影响
- 新店入驻：建库 5–7s + provision 11–15s；跨店登录被拒；退订后请求 fail closed
- 分析沙箱：建库约 10s，上传 5 个原始文件约 7s，Agent 约 3.5 分钟得出正确根因
- `embedding()` 与 Bedrock Titan v2 的向量一致（最大余弦距离 4e-7）

## 安全提示

- `.env` 和 `.tenants.json` 已在 `.gitignore` 中。DSN 是 admin（superuser）凭证，因为 fs9 和 `embedding()` 都要求 superuser。生产中应把 SQL 工具和文件 / 向量工具拆成不同角色。
- **租户从哪里来**：demo 里 `store_id` 来自请求 payload；生产必须从调用方已验证的身份里取（AgentCore inbound JWT 授权里的 `store_id` claim），否则一家店可以冒充另一家。未知店一律拒绝，不会回落到默认库。
- `query_data` 用 `SET TRANSACTION READ ONLY`，并且只接受单条 SELECT/WITH；`recall` 和 `remember` 强制按 user 过滤；`query_data` 的 user 过滤目前只靠 prompt 约束，生产应加 RLS。
- 分析沙箱里 Agent 有完整权限，这在一次性库里是可以接受的，但 db9 的 SQL 能发 HTTP 请求（`http_get`），被注入的上传文件理论上可以诱导 Agent 外传数据。生产应限制沙箱的出网能力（我没有找到 db9 关闭该功能的开关，未验证）。
- 匿名 db9 账号的 bearer token 7 天过期（CLI 会自动刷新）；正式使用请 `db9 claim`。
