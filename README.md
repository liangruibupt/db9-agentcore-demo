# db9 × Amazon Bedrock AgentCore Demo

一个做成 **多店 SaaS** 的客服 Copilot：同一套 Agent 代码（**Strands Agents + Bedrock Claude**，跑在 **Amazon Bedrock AgentCore Runtime** 上）服务很多家店，每家店是一个租户，拥有**自己独立的 [db9](https://db9.ai) 数据库**——知识库、订单、顾客记忆、会话快照、运行轨迹、报告全在里面。结构化数据进表，原始上下文进文件（fs9），两者都能用 SQL 查。

另外三个场景回答「什么时候非 db9 这类产品不可」（分析见下文「深挖」一节）：

- **Demo 07 帮用户生成应用的 Agent 平台**：每个生成的应用一个库，Agent 自己建表、写 API、测试，分支迭代后再上线（`scripts/07_app_builder.py`）。
- **Demo 08 Agent 评测 / 强化学习环境**：每个 episode 一个全新的店铺世界，Agent 真实执行写操作，用 SQL 检查最终状态（`scripts/08_eval_rl_env.py`）。
- **Demo 06 一次性分析沙箱**：原始 CSV / JSONL 不建表直接用 SQL 查（`scripts/06_analyst_sandbox.py`）。

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

## 深挖：db9 真正不可替代的是哪类场景

先纠正一个常见误解：「几秒建一个新库」和「不建表直接用 SQL 查 CSV / JSON」**单独拿出来都不是 db9 独有的**：

- 在一个 Aurora 集群里 `CREATE DATABASE ... TEMPLATE`，小库一两秒也能建好；
- Athena、Redshift Spectrum、DuckDB 早就能直接查 S3 或本地的 CSV / JSON。

所以要问的是：**什么业务同时需要这些能力，而替代方案在叠加之后失灵？**

### 什么场景需要「几秒钟给每个任务开一个新库」

共同特征：建库的是程序或 Agent，不是 DBA；库数量巨大、单个很小、多数很快被丢弃；每个库要有独立凭证、独立生命周期，有时还要能分支。

| 场景 | 为什么要「每个任务一个库」 | 为什么 Aurora / DynamoDB 不合适 |
|---|---|---|
| **帮用户生成应用的 Agent 平台**（Replit、Lovable、Bolt 一类；Demo 07） | 每个生成的应用需要自己的 Postgres 和可交给用户的连接串；规模几万到几百万，绝大多数是试完就扔的原型 | 一应用一集群：几分钟、保底成本、区域配额；挤进共享集群：连接数、吵闹邻居、单库凭证 / 删除 / 计费 / 分支都难做；DynamoDB 不是生成代码需要的 Postgres。Neon 公开说过它平台上大部分新库由 AI Agent 创建，Replit Agent 背后就是 Neon |
| **Agent 评测 / 强化学习环境**（τ-bench 一类；Demo 08） | 每一轮都要从同一份初始世界开始，Agent 会写库，轮与轮不能互相污染；几百到几千轮并行；世界里还有文件（KB、轨迹） | 共享集群里每轮一个库：并行几千轮挤爆算力和连接，文件状态还要在 S3 另外重置；每轮一个 clone：几分钟、按实例计费；DynamoDB 承载不了关系型世界，也回答不了打分程序的 SQL |
| 编码 Agent 改数据库结构（每个 PR / 每次尝试一个分支） | 在和生产一致的副本上跑迁移和测试，跑完扔掉 | Aurora Fast Clone 也能做，但每个 clone 是新集群。**这里 db9 不占优**：它的分支是全量复制（小库实测约 60 秒），不如 Neon / Xata 的写时复制 |
| 给 AI 应用的每个终端用户一个私有库 | 百万级用户、每人数据很小、大部分时间闲置；物理隔离最好解释、销户即删库 | 百万个集群不现实；共享集群回到 RLS + S3 前缀 + 清理脚本；DynamoDB 能按用户分区，但没有 SQL 分析、向量和文件 |

### 什么场景需要「不建表直接用 SQL 查 CSV / JSON」

共同特征：数据结构在写入之前没人知道、经常变；写入方是 Agent 或外部系统，没有人维护 DDL。

| 场景 | 例子 | 本仓库里 |
|---|---|---|
| Agent 自己产生的数据 | 每个工具返回的 JSON 都不同，新增工具就多一种结构；轨迹要和 `agent_runs` 等业务表在同一条 SQL 里 join | 02 的 JSONL 轨迹聚合；08 的打分程序读每个 episode 的轨迹；08 的评测历史 `/evals/*.jsonl` |
| 用户在对话里临时上传文件 | 「帮我分析这个表格」「把这份报价和我们的订单对一下」，结构上传后才知道 | 06 |
| Agent 从外部抓回的半结构化数据 | 第三方 API、webhook、网页抓取，结构随对方变化；先原样落盘，再用 SQL 抽字段 | — |
| 多个 Agent 之间交接数据 | A 生成 CSV，B 接着查；文件就是交接协议 | — |

需要承认：如果只是「一个 Agent 在一个会话里分析几个上传文件」，**DuckDB 跑在 AgentCore 会话自己的 microVM 里就够了**，不需要 db9。06 这个场景用来展示 fs9 能力没问题，但它不是 db9 不可替代的场景。

### 交集：db9 明显胜出的条件

只有当下面三点**同时**出现时，db9 才明显优于 Aurora / DynamoDB，也优于 DuckDB / Athena：

1. 由程序批量创建大量小而短命的库，每个库要有独立凭证，能删、能分支；
2. 库里同时有可事务写入的表和结构未知的文件，需要在同一条 SQL 里 join，并作为**一个整体**被复制、重置或删除；
3. 这个库要被多个进程 / Agent 通过网络共享（生成的应用和 Agent 同时连；评测时被测 Agent 和打分程序都要连）——这是 DuckDB 的边界。

最贴合的两个场景就是下面的 Demo 07 和 Demo 08。

## Demo 07：帮用户生成应用的 Agent 平台

`scripts/07_app_builder.py`：两个用户各说一句话，平台各给一个新库，Builder Agent（Claude Sonnet 4.6）在库里自己设计 schema、跑迁移、写种子数据、把应用 API 写成命名的参数化 SQL，并逐个测试。生成的源码（`schema.sql`、`seed.sql`、`api.json`、`README.md`）存在**同一个库的 fs9 里**，「一个应用」就是一个自包含单元。

```
user: 「做一个露营装备租赁预约系统」 ─► db9 create app-camp-rental ─► Builder Agent: DDL / 触发器 / seed / API / 测试
user: 「做一个跑团活动报名系统」     ─► db9 create app-run-club    ─► Builder Agent（并行）
                                                     │
platform: 用 api.json 里的 example_params 逐个跑接口（事务内执行后回滚）
user: 「加押金字段」 ─► db9 branch（表 + 数据 + /app 源码一起复制）─► Agent 在分支上迁移
platform: 分支上接口全过 + 老接口的名字和返回列都还在 ─► 把 /app/migrations/002_*.sql 重放到线上 ─► 删分支
没人用的原型 ─► db9 delete
```

实测（2026-10-09）：

| 步骤 | 结果 |
|---|---|
| 建库 | 3.7–6.0s / 个 |
| 构建（两个应用并行） | 露营租赁 164s：3 张表 + 防超订触发器，10/10 个接口通过；跑团报名 153s：3 张表 + 报名 / 取消函数（满员进候补、取消后候补自动递补），8/8 通过；Agent 在构建中遇到并自己修好了 1–2 条 db9 兼容性报错 |
| 迭代 | 分支 43–59s 变为 ACTIVE；Agent 131s 完成迁移和回填；分支 10/10 接口通过，10/10 老接口向后兼容；迁移重放到线上后，线上 10/10 通过 |
| 门禁拦截 | 第一次试跑时，Agent 用会写数据的方式测试接口，导致 `register_customer` 的示例参数撞唯一键，并且改名了老接口的返回列。平台门禁判定不通过，**线上应用没被动**。之后加了「测试必须回滚」的 `test_sql` 工具和向后兼容检查 |

为什么这个场景只有 db9 这类产品合适：见上面的对比表第一行。另外，「应用 = 一个库（数据 + 源码）」让分支、交付、删除都是一次调用；用 Aurora 要把源码另放 S3 / Git，分支时两边各自处理。

## Demo 08：Agent 评测 / 强化学习环境

`scripts/08_eval_rl_env.py`：上线客服 Copilot 之前（或者做 RL 训练时），让它跑一批 τ-bench 风格的任务。Agent 会**真的执行写操作**（取消订单、退货退款、改地址），打分程序检查**最终数据库状态**，而不是看回答写得好不好。顾客由一个 LLM 用户模拟器扮演，可以多轮对话，Agent 可以先确认再动手。

```
for each task (并行 4 个):
    db9 create ep-xxx  ─► provision 店铺模板（KB 文件 + 向量 + 订单）+ 顾客 / 送达日期 / 退款流水
    LLM 用户模拟器 ⇄ 客服 Agent（search_knowledge · get_order · cancel_order · return_order · ...）
    打分：SQL 检查订单状态、退款流水（金额 + 方式）、无关订单没被动 ；读 fs9 里的 JSONL 轨迹统计工具调用
    db9 delete ep-xxx
结果 ─► 平台库 /evals/<run>.jsonl ─► SQL 跨批次、跨模型对比
```

8 个任务覆盖：取消处理中订单（全额退回原支付方式）、不可退商品、会员退用过的帐篷（店铺积分，扣 15%，即 279.65）、改地址、取消已发货订单（应拒绝）、超出退货期（应拒绝）、替别人取消订单（应拒绝）、退回未使用商品（全额退款）。

实测（2026-10-09，Claude Sonnet 4.6）：

| 指标 | 结果 |
|---|---|
| 每个 episode 的世界准备时间 | 18–33s（建库 4–7s + 写入模板、在 SQL 里向量化 KB、补充世界状态 14–26s） |
| 8 个 episode，4 个并行 | 墙钟 128s（串行合计 409s）；每个 episode 的库用完即删 |
| 第一批：单轮对话 | pass@1 = 7/8。失败的「会员退帐篷」：Agent 算对了 279.65 店铺积分，但先问「要我帮你办吗？」，单轮任务里没人回答，状态没变 |
| 第二批：加 LLM 用户模拟器 | pass@1 = **8/8**（帐篷任务第 2 轮确认后执行 `return_order`） |
| 评测历史 | 两批结果都在平台库 `/evals/*.jsonl`，一条 `SELECT ... FROM extensions.fs9('/evals/*.jsonl') GROUP BY run, model` 即可对比 |

为什么这个场景 db9 合适：评测 / RL 要成百上千次「把世界重置到初始状态」，并且并行跑；世界里不只有表，还有 KB 文件和 Agent 写下的轨迹。db9 一次建库拿到完整、独立、带文件的世界，用完一次删除。Aurora 要么在共享集群里挤（算力、连接、文件状态另管），要么每轮一个 clone（几分钟、按实例计费）；DynamoDB 承载不了这个关系型世界，也回答不了打分 SQL。

局限：db9 的分支是全量复制（约 60 秒），比「新建 + 灌模板」（约 20–30 秒）还慢，所以这里用后者重置世界。如果 db9 未来支持写时复制分支，重置可以降到秒级以下。匿名账号最多 5 个库，所以并行度被限制在 4。

## Demo 06：一次性分析沙箱（fs9 能力展示）

`scripts/06_analyst_sandbox.py`：店主把一堆**原始导出文件**（订单 CSV、退货 CSV、物流 JSONL、评论 JSONL、一封供应商邮件）扔给平台，问「9 月退货率为什么涨了这么多？」。平台为这一个问题新建一个库，文件原样放进 fs9（不定义 schema，不写 ETL）；Agent 在库里拥有完整权限，自己 `CREATE TABLE AS SELECT ... FROM extensions.fs9('/uploads/x.csv')`，自己 join、聚合、读邮件，最后写 `/out/report.md`；用完删库。

实测：约 3.5 分钟、29 次工具调用（4 条 SQL 报错后自己改正），找到数据生成器里埋下的答案——主因是 9 月 8 日起发货的 **B-0908 批次背包腰带扣缺陷**（该批次退货率 39.4%，旧批次 8.3%；剔除后 9 月退货率 4.99%，与 8 月 5.06% 持平），次因是 QuickShip 9 月配送时效从 2.9 天拉长到 4.7 天。

定位说明：这个场景 Aurora 确实不合适（不支持 `file_fdw`，`aws_s3` 导入要先建好表，共享集群上给 LLM DDL 有风险），但**单会话、单 Agent** 的文件分析用 DuckDB 跑在 AgentCore 会话里就够了。只有沙箱要被多个进程共享、或要在会话结束后保留时，db9 才明显更好。

实测中观察到的 db9 局限：一条把 2,146 行 JSONL 物化成表的 `CREATE TABLE AS` 超过 60 秒超时（重试成功），几条多表 join 返回 `internal error`（Agent 改写后成功）。兼容性和性能还不稳，适合沙箱，不适合关键链路。

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
| 9 | **一应用一库的应用生成平台**：Agent 建 schema / 触发器 / API，源码存进同一个库，分支迭代 + 兼容性门禁 + 上线 | `scripts/07_app_builder.py`、`db9_agent/provision.py` |
| 10 | **一 episode 一库的评测 / RL 环境**：并行重置世界、真实写操作、SQL 打分、评测历史存 JSONL 用 SQL 汇总 | `scripts/08_eval_rl_env.py`、`db9_agent/actions.py` |
| 11 | **AgentCore Runtime 契约**：`/ping` + `/invocations`，从 AgentCore 的 session header 取 session id，DSN 从 Secrets Manager 读取 | `agentcore_app.py`、`db9_agent/db.py` |

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
.venv/bin/python scripts/07_app_builder.py      # 应用生成平台（约 6–8 分钟，最多同时占 3 个库；--keep 保留露营租赁应用）
.venv/bin/python scripts/08_eval_rl_env.py      # 评测环境（约 2–3 分钟；并行度 = 5 - 已有库数，或 --workers N）

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

- SQL 内入库：Nimbus Gear 5 个 md 文件 → 9 个 chunk，连同 schema、配置、订单整个 provision 约 8s
- Agent 一轮对话 9–16s（Claude Sonnet 4.6，3–4 次工具调用）；工具延迟：`query_data` 约 0.5s，`search_knowledge` 约 0.9s，`remember` 约 1.8s
- 新会话（空聊天历史）能通过 `recall` 拿回上一会话记住的「Nimbus Plus 会员」「西雅图自提柜」，并据此给出 60 天退货窗口
- 分支：请求 0.3s 返回，约 64s 变为 ACTIVE；fs9 的 14 个文件随分支一起复制；分支上把 chunk 改小后，检索距离从 0.54 降到 0.38，生产库不受影响
- 新店入驻：建库 5–7s + provision 11–15s；跨店登录被拒；退订后请求 fail closed
- 分析沙箱：建库约 10s，上传 5 个原始文件约 7s，Agent 约 3.5 分钟得出正确根因
- 应用生成平台：建库 4–6s；两个应用并行构建 153–164s，接口 8/8、10/10 通过；分支迭代 + 兼容性门禁 + 上线全部通过
- 评测环境：每个 episode 世界准备 18–33s；8 个 episode 4 并行墙钟 128s；pass@1 = 8/8（多轮用户模拟器）
- `embedding()` 与 Bedrock Titan v2 的向量一致（最大余弦距离 4e-7）

## 安全提示

- `.env` 和 `.tenants.json` 已在 `.gitignore` 中。DSN 是 admin（superuser）凭证，因为 fs9 和 `embedding()` 都要求 superuser。生产中应把 SQL 工具和文件 / 向量工具拆成不同角色。
- **租户从哪里来**：demo 里 `store_id` 来自请求 payload；生产必须从调用方已验证的身份里取（AgentCore inbound JWT 授权里的 `store_id` claim），否则一家店可以冒充另一家。未知店一律拒绝，不会回落到默认库。
- `query_data` 用 `SET TRANSACTION READ ONLY`，并且只接受单条 SELECT/WITH；`recall` 和 `remember` 强制按 user 过滤；`query_data` 的 user 过滤目前只靠 prompt 约束，生产应加 RLS。
- 分析沙箱和应用生成平台里，Agent 在一次性库里有完整权限，破坏半径限于那个库；但 db9 的 SQL 能发 HTTP 请求（`http_get`），被注入的上传文件或用户需求理论上可以诱导 Agent 外传数据。生产应限制沙箱的出网能力（我没有找到 db9 关闭该功能的开关，未验证）。07 里 Agent 生成的 SQL 只在它自己的库里执行，平台不执行 Agent 生成的应用代码。
- 08 的写操作工具在代码里强制「只能动当前顾客自己的订单」，并检查订单状态（只能取消处理中订单、只能退已送达订单）；退不退、退多少、退到哪里由 Agent 按政策决定，这正是评测要测的部分。
- 匿名 db9 账号的 bearer token 7 天过期（CLI 会自动刷新）；正式使用请 `db9 claim`。
