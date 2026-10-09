# db9.ai 调研报告

> 调研日期：2026-10-09。资料来源：db9.ai 官网/文档/FAQ、github.com/db9-ai、dbdb.io（CMU），以及本仓库 demo 中的实测结果。标注「实测」的结论是在本机用 db9 CLI 2.2.4 跑出来的。

## 1. db9 是什么

一句话：**给 AI Agent 用的 Serverless PostgreSQL，外加一个可以用 SQL 查询的云文件系统。**

- **团队背景**：PingCAP 团队出品（TiDB/TiKV 的作者），定位是 "A Database for Agents, Built by Agents"（CMU Andy Pavlo 有一期同名 talk）。
- **架构**（官方 Architecture 文档）：
  - 数据面 `db9-server`：Rust 写的 SQL 引擎（sqlparser-rs + Tokio），说 PostgreSQL wire protocol，`version()` 返回 `PostgreSQL 17.0 (db9-server 0.1.0 … on TiKV)`（实测）。
  - 控制面 `db9-backend`：Go 写的 REST API，负责建库、鉴权、分支、可观测性。
  - 存储层：**TiKV**（Raft + 分布式事务 KV）。每个数据库是 TiKV 里一个独立 keyspace，多租户共享集群、数据隔离——所以建库只是「分一个 keyspace」，不用起实例。
  - **部署位置（实测）**：`pg.d0000.db9.io` 和 `api.db9.ai` 解析到 `*.elb.us-west-2.amazonaws.com`，即跑在 **AWS us-west-2 的 EKS** 上；目前只看到 us-west-2 一个 region。
- **核心能力**（都编译在 server 里，纯 SQL 调用）：

| 能力 | 用法 | 备注 |
|---|---|---|
| 秒级建库 / 匿名库 | `db9 create`、TS SDK `instantDatabase()` | 无需注册即可用，匿名账号上限 5 个库；`db9 claim` 解除（官方称免费） |
| 内置向量 + 自动 embedding | `embedding('text')`、`VEC_EMBED_COSINE_DISTANCE(col,'text')`、HNSW | pgvector 兼容；只有「内联字面量 + LIMIT + 无 WHERE」才走 HNSW 索引 |
| `CHUNK_TEXT()` | 表函数，markdown 感知切块 | 文件 → 切块 → 向量 → 入表 一条 SQL 完成（见 demo step 1） |
| **fs9 文件系统** | `extensions.fs9('/logs/*.jsonl')` 把 CSV/JSONL/Parquet 当表查；`fs9_write/append/read`；CLI `db9 fs cp/sh/mount`（FUSE） | 单文件 100 MB；同一个库里「表 + 文件」共存，分支时一起复制 |
| HTTP from SQL | `http_get/http_post(...)` | 仅 HTTPS、防 SSRF、5s 超时、1 MB 响应 |
| 分支 | `db9 branch create` | **全量复制**（含表、文件、cron、用户），异步；实测小库约 60 秒 ACTIVE。不是 Neon 那种 CoW 瞬时分支 |
| pg_cron | `cron.schedule(...)` | **当前版本不可用**（文档：`PreActivationSeal`） |
| Serverless Functions | `db9 functions create`，JS/TS 运行时，`ctx.fs9` | |
| Agent 集成 | `db9 onboard --agent claude/codex/opencode`，`https://db9.ai/skill.md` | 给 coding agent 装 skill，让 agent 自己建库/查库 |

## 2. 开源还是闭源？

**核心是闭源托管服务（SaaS），底座开源。**

- `github.com/db9-ai` 公开的只有：示例（db9-examples）、GitHub Action（ephemeral DB for CI）、wiki9、paste9、FUSE docker 示例、`auth9token-go`，以及 juicefs / rocksdb / rust-rocksdb 的 fork。**没有 db9-server、db9-backend 的源码**，文档里也没有自托管 / BYOC 选项。
- CLI 是从 `db9.ai/releases` 下载的二进制。
- 存储层 TiKV 是 Apache-2.0、CNCF 毕业项目——但「TiKV 开源」≠「db9 开源」。
- 官网/文档未找到公开定价页；限额页写 claim 账号「no payment required」。SLA、合规认证（SOC2 等）、多 region、备份 RPO/RTO 在文档里我没找到明确承诺——生产使用前需要和厂商确认。

## 3. 与 Aurora、DynamoDB 的差异

| 维度 | **db9** | **Aurora PostgreSQL**（含 Serverless v2） | **Aurora DSQL** | **DynamoDB** |
|---|---|---|---|---|
| 定位 | Agent 的「数据库 + 文件系统」一体工作区 | 企业级托管 Postgres | 无服务器分布式 Postgres 兼容、多活 | Serverless KV / 文档库 |
| 数据模型 | 关系 + 向量 + 文件（fs9） | 关系（+ pgvector 扩展） | 关系 | KV / 文档，无 JOIN |
| 兼容性 | PG wire 协议，自研引擎；缺 PostGIS / pg_trgm / pgcrypto / 逻辑复制 / 分区表；最高只有 REPEATABLE READ | 原生 PostgreSQL，扩展生态最全 | PG 兼容子集，不支持 `CREATE EXTENSION`（因此没有 pgvector） | 自有 API / PartiQL |
| 建库速度 | 官方称 <1s；实测 CLI 端到端约 3.7–4s | 新建集群分钟级 | 秒级 | 建表秒级 |
| 向量 / embedding | 内置 `embedding()`，SQL 里直接生成 | pgvector + `aws_ml` 扩展在 SQL 里调 Bedrock | 无 | 无（需 zero-ETL 到 OpenSearch） |
| 文件 | 内置 fs9，文件可直接被 SQL 查询 | 无（`aws_s3` 扩展导入/导出 S3） | 无 | 无（大对象放 S3） |
| 分支 / 克隆 | 全量复制，分钟级，带文件 | Fast Clone（CoW，快） | — | 无（备份恢复 / 导出） |
| 规模与可靠性 | TiKV 可水平扩展；SLA 未公开；目前只看到 us-west-2 | Multi-AZ、Global Database、PITR、只读副本、SLA 99.99% | 多 region 多活 | 无限扩展、Global Tables、SLA 99.999%（全局表） |
| 网络 / 安全 | 公网端点 + TLS + 密码 / token；无 VPC 私有接入 | VPC 内、IAM 认证、KMS、PrivateLink | IAM 认证 | IAM、VPC endpoint |
| 商业模式 | 闭源 SaaS，目前免费 | AWS 按 ACU / 实例 + 存储计费 | 按 DPU + 存储 | 按请求 / 容量 |

**怎么选**：
- db9 的强项是**开发和 Agent 的体验**：一行命令拿到带向量、文件、HTTP 的 Postgres；「一个任务 / 一个租户一个库」用完就丢；Agent 的记忆、产物、日志放一个地方，用 SQL 统一查。适合原型、Agent 沙箱、CI 临时库、长尾多租户的小库。
- Aurora / DynamoDB 的强项是**生产级 SLA、合规、VPC 隔离和完整生态**。核心业务数据、强一致和合规要求高的负载仍然该放在这里。
- 常见组合：业务主数据放 Aurora / DynamoDB；db9 只做 Agent 的 scratch / workspace 层；或者把 AgentCore Memory + S3 + Aurora pgvector 拼起来，实现 db9 一体化提供的功能。

## 4. 竞品

| 类别 | 产品 | 和 db9 的关系 |
|---|---|---|
| Serverless Postgres for agents | **Neon**（2025 年被 Databricks 收购） | 最直接的竞品：CoW 瞬时分支、scale-to-zero、扩展最全；没有内置文件系统 / embedding |
| | **Xata**（已开源） | 主打 agent sandbox：CoW 分支、按需算力 |
| | **Tiger Data Agentic Postgres**（TimescaleDB） | 快速 fork、MCP、混合搜索 |
| | **Supabase** | 开源全栈平台（Auth / Storage / Realtime / Edge Functions + pgvector）；db9 只对标它的数据库层 |
| | Prisma Postgres、PlanetScale Postgres、Nile（租户虚拟化 PG） | 托管 Postgres，侧重 DX / 多租户 |
| 同门 | **TiDB Cloud Starter / TiDB X**（PingCAP） | MySQL 兼容，带向量和多租户 agent 场景；db9 相当于 PingCAP 在 Postgres 生态的 agent 产品 |
| 每租户一库 / 文件 | **Turso**（libSQL / SQLite，db-per-tenant）+ **AgentFS** | Agent 文件系统 + 数据库的理念和 fs9 最像 |
| Agent 记忆层 | **Amazon Bedrock AgentCore Memory**、Mem0、Zep、Letta | 只管记忆，不是通用数据库；db9 用「表 + 文件」自建记忆 |
| 向量库 | Amazon S3 Vectors、OpenSearch、Pinecone、LanceDB | 只解决向量检索 |
| AWS 自家对应 | Aurora PostgreSQL + pgvector + `aws_ml`、Aurora DSQL、DynamoDB、S3 | 组件化拼装 |

## 5. 实测发现（本仓库 demo）

1. **SQL 内完成 RAG 入库**：fs9 目录 → `CHUNK_TEXT` → `embedding()` → 插表，一条 SQL；4 个文件 5 个 chunk 约 2s。
2. **`embedding()` 的向量与 Amazon Bedrock Titan Text Embeddings v2 一致**：同一批 chunk 两种方式生成的向量，最大余弦距离约 4e-7（见 `scripts/05_bedrock_embeddings.py`）。文档写的默认模型是 `text-embedding-v4`，但至少在 us-west-2、本次测试中，结果与 Titan v2 数值一致。这只是观测结果，厂商没有承诺，后端随时可能换。
3. **分支**：创建请求 0.3s 返回，`CLONING` → `ACTIVE` 约 60–64s（一个很小的库）；fs9 文件随分支一起复制。
4. **建库**：CLI 端到端约 3.7–4s（含 CLI 进程启动和 token 刷新），与「<1s」的宣传有差距，但仍比新建 Aurora 集群快两个数量级。
5. **坑**：HNSW 只在「内联字面量」查询时生效（绑定参数会退化为全表扫描）；`fs9_write` 不会自动建父目录；`fs9_append` 不会补换行；`http_get` 表函数形式要加 `extensions.` 前缀；fs9 / embedding 需要 superuser（admin）；pg_cron 当前不可用。
