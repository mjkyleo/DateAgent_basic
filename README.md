# 数据分析Agent MVP（basic/）

按《[数据分析Agent MVP 设计文档](数据分析Agent%20MVP%20设计文档.md)》V1.0 实现的最小可用端到端数据分析 Agent。
**MySQL only · 只读 · 双层人工确认 · 每步可解释**——系统默认不会自动执行任何数据库操作。

> 本目录是与仓库根「完整版 da-lite」**并列的一套独立实现**：根版是 LangGraph + DuckDB 的完整方案
> （见根 `README.md`），本 MVP 刻意不依赖那些重型包，单独跑、单独测。
> 两者共享同一批 kaggle 测试库，但端口、存储、进程互相独立。

## 快速开始

```bash
# 0) 配置 .env（复制 .env.example 填写；LLM 留空则自动走规则兜底，可离线演示）
cp .env.example .env

# 1) 后端（仓库根执行，:8001）
python -m uvicorn basic.main:app --port 8001

# 2) 前端（:5174，代理 /api → 8001）
cd basic/web && npm install && npm run dev
# 打开 http://localhost:5174
```

Windows 一键起停（双击即可，后台窗口最小化，日志落在 `basic/uvicorn.log`、`basic/vite.log`）：

```bat
basic\scripts\start_all.bat    rem 选 Python → 起后端/前端 → 轮询 /healthz 直到就绪
basic\scripts\stop_all.bat     rem 按端口 8001/5174 杀进程树 + 起脚本开的标题窗口
```

测试：`cd basic && python -m pytest tests -q`（147 项：安全门/统计洞察/存储适配器/错误脱敏/轮次持久化/
文件导入/适配器生命周期/双层确认落库/加固回归/运行时修复回归（事件循环/双份初始化/降级绑定/级联清理/
元数据库凭据复用/降级 503）/内容画像事实层（脱敏、预算、降级、缓存、phenomena、叙述超时下限）/供应商模型清单拉取/
任务清单（CRUD、调序、批量执行的混合语义、确认门、只读安全门不可绕过、账号脱敏）/
文件源端到端/备用模型切换矩阵）。

依赖：`pip install -r basic/requirements.txt`。

一次性工具（`basic/scripts/`）：

```bash
python -m basic.scripts.migrate_sessions_to_mysql [--dry-run]   # SQLite 历史会话 → MySQL 元数据库（幂等）
python -m basic.scripts.verify_new_features                     # 新增能力自检（41 项断言，需真实 MySQL）
```

## 数据入口（提问框左下角）

全部"数据进来"的操作集中在提问框左下角：

- **数据源**：新增 MySQL 连接（建议仅 SELECT 权限账号）、测试连通、切换激活、删除；
- **库表结构**：浏览库 → 表 → 字段，勾选后作为提问上下文；
- **导入文件**：上传 CSV / TSV / TXT / JSON / XLSX（≤20MB、≤20 万行、≤100 列），
  解析成一张 SQLite 表并**自动激活**，上传完直接提问。列名保留原文（含中文），
  类型自动推断（整数/小数/文本，空串→NULL）；文件源以只读模式打开（SQLite URI `mode=ro`）。
  上限走配置：`MAX_UPLOAD_MB` / `MAX_IMPORT_ROWS`。
- **模型**：接入多家大语言模型供应商（见下）。

## 项目（会话分组）

**一个项目 = 一个本地文件夹**。项目只存「名称 + 文件夹路径」，会话正文仍在 MySQL
（`sessions.project_id` 关联）；文件夹是项目的产物目录，可以整体拷走。

- 新建项目不填路径时，自动在 `PROJECTS_DIR/<项目名>` 下建目录；填了就沿用该目录（不存在会创建）。
- 名称默认取文件夹名。
- 首次启动自动建「未分组」（默认项目），历史会话全部归入它，`project_id` 永不为空。
- **删项目不删会话、不删文件夹**：会话移回「未分组」，磁盘目录原样保留。

| 接口 | 用途 |
|---|---|
| `GET /api/projects` | 列表（含每个项目的会话数） |
| `POST /api/projects` | 新建 `{name?, folder_path?}`（都给空则报 422） |
| `PUT /api/projects/{id}` | 改名 / 改路径（「未分组」不可改名） |
| `DELETE /api/projects/{id}` | 删除（会话移回未分组，「未分组」不可删） |

会话侧：

| 接口 | 用途 |
|---|---|
| `GET /api/agent/rounds?project_id=` | 按项目筛选历史 |
| `PATCH /api/agent/rounds/{id}` | 重命名（`{title}`，不动 `user_question`） |
| `PATCH /api/agent/rounds/{id}/project` | 移动到别的项目（目标必须存在） |
| `DELETE /api/agent/rounds/{id}` | 删除（前端必弹确认模态框） |

左栏按项目分组渲染（原生 `<details>`，默认展开），每条会话三个内联操作：重命名 / 移动 / 删除。
当前项目记在 `localStorage`，刷新不丢；**提问时归属当前项目**，项目被删则自动回落。

## 任务清单（把建议变成可执行待办）

第一步的「数据源洞察」和第三步的「SQL 建议」给出的优化项，不再只是读完就过：
每条建议右侧有「加入清单」，落进**按项目一份**的待办列表，人可以接着编辑、调序、批量执行。

- **混合语义**（对应确认的方案）：绑了只读 SQL 的任务真的去查；只说了"该补索引"这类
  没法用 SELECT 表达的任务，逐条给出跳过原因，做完由人点「标记完成」——系统不代替人办结。
- **确认位不可省**：`/run` 没带 `confirmed: true` 直接 409。前端也不是点一下就跑：
  先把待执行的 SQL 摊开、勾一次「我已核对以上 N 条 SQL」，按钮才亮。
- **执行仍走同一道安全门**：批量执行调的是 `QueryExecutor`，SELECT-only 校验、超时、
  行数封顶、错误脱敏全部照旧；`DELETE ...` 混在清单里只会被标成失败并说明原因。
- **出处不悄悄换**：任务绑定的数据源被删了，就跳过并说明，**不会**退回当前激活源去查——
  建议来自 A 库、结果却是 B 库的数字，是最坏的一种"成功"。
- 每条被执行的任务都会留下一轮会话（`intent=任务清单批量执行`），界面「查看结果」直接跳回看。

| 接口 | 用途 |
|---|---|
| `GET /api/tasks?project_id=` | 本项目清单（按 `sort_order` 升序） |
| `POST /api/tasks` | 新建 `{project_id, title, note?, source_kind?, source_ref?, question?, sql?, ds_id?}` |
| `PUT /api/tasks/{id}` | 编辑（只提交改过的字段；`status` 只能人写 todo/done/failed） |
| `DELETE /api/tasks/{id}` | 删除 |
| `POST /api/tasks/reorder` | 提交整份顺序 `{project_id, ids}`（↑↓ 每点一次提交全部，避免逐条 ±1 的并发错位） |
| `POST /api/tasks/run` | 批量执行 `{project_id, task_ids?, confirmed}`；`task_ids` 留空 = 本项目全部未完成 |

- `source_ref` 存的是**当时的证据快照**（规则码、severity、依据、库表），建议原文之后变了
  也不影响任务说得清"当初为什么加它"。
- 单次最多 50 条：一条查询有超时兜底，几十条串起来会把请求拖到网关超时、前端只剩空白错误，
  宁可当场拒绝并提示分两批。

## 模型供应商

供应商是「一家厂商」，模型挂在它下面（1:N）—— 同一家的地址/密钥/协议相同，
变的是模型名，所以加模型只需填一个名字。

| 接口 | 用途 |
|---|---|
| `GET /api/providers` | 列表（含各自模型；密钥只回 `has_key`） |
| `POST /api/providers` | 接入 `{provider_key, name, api_base, api_key, protocol?, models?}` |
| `POST /api/providers/test` | **草稿测试**（表单未保存时用）；`model` 留空只验端点+密钥 |
| `PUT /api/providers/{id}` | 编辑 / 启用禁用（`api_key` 传空串 = 保持原密钥） |
| `DELETE /api/providers/{id}` | 删除（连带模型；若删的是当前模型自动顶上别的） |
| `POST /api/providers/{id}/test` | 测试已保存的（默认测它的默认模型） |
| `POST /api/providers/{id}/models` | 登记模型名 |
| `POST /api/providers/{id}/models/batch` | 批量登记（从拉来的清单里勾，已存在的跳过） |
| `GET /api/providers/remote-models` | 按 `LLM_API_BASE/KEY` 拉厂商模型清单，无需先接入 |
| `GET /api/providers/{id}/remote-models` | 用该供应商存量密钥拉清单（密钥不外泄） |
| `DELETE /api/providers/{id}/models/{mid}` | 移除模型 |
| `POST /api/providers/{id}/models/{mid}/activate` | 设为 Agent 当前使用的模型 |
| `GET /api/providers/active` | 当前 (供应商, 模型) |

拉清单只证明"列表里有这个名字"，**不证明这把密钥能调它**（实测过一个 Key 列出 178 个模型、
逐个调全 `403 AccessDenied.Unpurchased`）；能调与否仍以「测试」按钮为准。

**请求地址 = 接口地址 + 接口协议路径**，所以「接口地址」要含版本前缀：

```
https://api.deepseek.com/v1            + /chat/completions
https://api.moonshot.cn/v1             + /chat/completions
https://open.bigmodel.cn/api/paas/v4   + /chat/completions
https://dashscope.aliyuncs.com/compatible-mode/v1 + /chat/completions
```

前端提供 8 个快捷模板（自定义 / MiMo API Key / MiMo Token Plan / DeepSeek / Kimi /
智谱 GLM / 千问 / MiniMax）。**MiMo 两个模板刻意只填名称、地址留空**——不确定的地址
填进去比空着更费时间排查。

解析优先级：`is_active` 的 (供应商, 模型) > 环境变量 `LLM_API_BASE/KEY/MODEL` > 规则兜底。
被禁用（`enabled=0`）的供应商不参与解析。

### 主模型不可用时自动切备用

配 `LLM_FALLBACK_MODEL` 即启用（同一端点、同一密钥，只换模型 ID）：

```
主模型失败
  ├─ 可重试（超时 / 连接失败 / 5xx / 429 / 响应结构异常 / 空内容）
  │     → 打 WARNING「切换至备用模型 X 重试」，换备用模型重试一次
  │            ├─ 成功 → 正常返回（用户无感）
  │            └─ 失败 → 抛备用模型的错误（retryable 保持原样）
  └─ 不可重试（400/401/403/404 —— 参数错 / 密钥无效 / 模型未授权）
        → 直接抛，不切换
```

**为什么 403 不切**：`AccessDenied.Unpurchased`、`invalid_api_key` 这类错误是「凭据或授权」
层面的事，同一把钥匙换个模型 ID 必然同样失败；重试只会让每次调用多等一轮超时，
把 3 秒的失败变成 6 秒。这类错误应该立刻走规则兜底，并由用户去修配置。

要换**端点/密钥**（而不只是模型 ID），用前端「模型管理」接入并激活另一个模型 ——
那里能分别测试连通性，比在同一组环境变量里猜更可靠。

验证方式（两条命令即可确认切换是否生效）：

```bash
# 1) 单测覆盖切换矩阵：可重试切、403 不切、备用也失败时抛谁的错、配置残缺时跳过
python -m pytest basic/tests/test_llm_fallback.py -q

# 2) 实测端点与备用模型的连通性（403/401 会直接打印出来）
python -c "from basic.config.settings import settings as s; from basic.core.llm import resolve_model, resolve_fallback, probe; print('主模型:', probe(resolve_model(None, s))); print('备用  :', resolve_fallback(s) and probe(resolve_fallback(s)))"
```

`probe()` 分两段，缺一不可：`GET /models` 只证明"钥匙能进大门"（端点可达、密钥有效），
**不能**证明"有该模型的权限"——本项目接入时就踩到了：一个 Key 能列出 178 个模型，
而每个模型调起来都是 `403 AccessDenied.Unpurchased`。所以第二段必须真发一次
`max_tokens=8` 的对话，才能证明该模型可调用。两段的 403 含义不同，分别报
「密钥无权限」与「模型未授权/未开通」。

## 存储：数据源在 SQLite，会话与模型在 MySQL

| 数据 | 存储 | 位置 |
|---|---|---|
| 数据源连接配置 | SQLite | `data/store.db`（文件源指向本机 `.db`，属本地运维配置） |
| 项目（名称 + 本地文件夹路径） | **MySQL** | 库 `data_agent.projects` |
| 问数会话（项目归属/标题/问题/意图/SQL/结果/洞察） | **MySQL** | 库 `data_agent.sessions` |
| 模型供应商 | **MySQL** | 库 `data_agent.llm_providers` |
| 供应商下的模型清单 | **MySQL** | 库 `data_agent.llm_provider_models` |
| 任务清单（建议转成的待办） | **MySQL** | 库 `data_agent.project_tasks` |

- 元数据库连接：`AGENT_MYSQL_HOST/PORT/USER/PASSWORD/DATABASE`（默认库名 `data_agent`）；
  **未配密码时复用当前激活 MySQL 数据源的凭据**（同一台服务器通常是同一套账号）。
- MySQL 不可用时启动**显式降级**为 SQLite 并打 ERROR 日志，不会静默把会话写进本地文件。
- 历史会话从 SQLite 迁过来：`python -m basic.scripts.migrate_sessions_to_mysql`（幂等，不覆盖已存在记录）。

## 会话的新建与删除

- **一轮 = 一条 session 记录**，`parse_intent` 阶段即写库（落问题/意图/目标库），中途取消也留痕。
- **新建会话**：左栏「新建」先落一条**空记录**（历史里虚线样式），归属**当前项目**；
  首次提问时由 `parse_intent` 带 `session_id` 绑定到它；已问过问题的会话不可被改写
  （`bind_question` 只认空会话），所以历史里不会凭空多出空白记录。
- **标题**：创建即按问题前 40 字自动生成，可随时重命名；老数据启动时补一次默认标题。
- **删除**：会话与项目都弹应用内确认模态框（显示被删对象、Esc 取消），不再用浏览器原生 `confirm()`。
- 回看：`GET /api/agent/rounds`（摘要倒序）/ `GET /api/agent/rounds/{id}`（完整记录）/
  `DELETE /api/agent/rounds/{id}`。

## SQL 优化能力

| 端点 | 触发位置 | 产出 |
|---|---|---|
| `POST /api/agent/source_insight` | 阶段①「解析意图」 | 数据源系统性描述 + 数据组织方式改进建议 |
| `POST /api/agent/sql_advice` | 阶段③「人工确认」编辑 SQL 时 | 该条 SQL 的查询优化方案 |

**规则引擎 + LLM 增强**（`core/sql_advisor.py` + `core/source_insight.py`）：

- **事实分两层**（`core/meta_tools.py`）：
  - 结构事实 —— 表/列/类型/注释/索引/行数估计，来自 information_schema 或 PRAGMA，便宜且每次一致；
  - 内容事实 —— `content_facts()` 逐列聚合出的行数、时间跨度、维度高频值、度量范围与均值、空值率。
    预算写死：`PROMPT_MAX_TABLES=6`、`PROMPT_MAX_COLUMNS=12`、`PROMPT_MAX_TOP_VALUES=5`、
    超过 `PROMPT_MAX_ROWS`（200 万行）的表跳过列级聚合（COUNT(DISTINCT) 会把提问路径拖死）。
- **发给模型的口径**：只发聚合结果 + 每列 ≤5 个高频取值，不发原始行；命中
  `SENSITIVE_RE`（手机号/邮箱/身份证/地址/姓名/token/password…，列名与注释都查）的列
  只报统计量、min/max/频次一律不发。频次分布排除 NULL —— 空值率单独说，
  否则模型会写出 `WHERE 城市 = 'None'`。
- **两类事实一起进 prompt**：`schema_context()` 带列注释与【数据内容事实】块，
  所以阶段①的描述和阶段②的 SQL 生成都看得到"这列实际是什么"，
  不再只凭列名猜（`_shape_fact` 按类型而不是列名分维度/度量：中文列名没有"金额"字样）。
- 规则层确定性：同一条 SQL + 同一份元数据 → 同一份结论。硬事实来自两处——
  SQL 结构走 sqlglot AST（与安全门同一个解析器），表元数据走适配器的
  `table_indexes()` / `table_row_estimate()`（MySQL 读 information_schema，文件源读 PRAGMA/COUNT）。
- 每条建议带 `evidence`（依据）；词表只收语义明确的词并整值精确匹配，
  纯 0/1 数值开关列不算状态列——猜错颜色/结论比不给更糟。
- LLM 只负责"把事实讲成业务语言"，提示词禁止编造表名/字段/数字；
  阶段①的产出多了一栏 `phenomena`（数据里真实可见的现象，每条必须带一个来自事实的数字）。
- **任何一层取不到都只降级**：画像失败 → 该表不进事实块、结构建议照跑；
  LLM 失败 → `overview` 回落规则摘要、`phenomena` 回落规则版（仍逐条摘自真实聚合值）；
  两者都缺 → 明确说"本次未取到内容画像"，不假装了解数据内容。
- 前端触发：**规则引擎即时自动跑**（编辑停下 debounce 700ms，不花 token）；第一步的洞察面板
  先只取事实层（`narrate=false`，秒级），拿到后**后台自动补取一次叙述**（`narrate=true`），
  失败或没配上模型时保留「AI 叙述」按钮手动重试 —— 事实层任何时候都先渲染，不等大模型。
- `source_insight` 按 (数据源, 库) 缓存（`SOURCE_INSIGHT_TTL_S`，默认 600s），前端可强制刷新；
  事实层与带叙述的结果分两个键（后者多 `:narr` 后缀），否则 2 秒能拿到的事实会被
  120 秒的叙述缓存拖着一等、或者反过来把没叙述的快结果污染进慢缓存。
  列级聚合另有 `META_CACHE_TTL_S`（默认 30s）的短缓存。整条链路的时间几乎都花在 LLM 上：
  实测 kaggle_demo（5 表 4.7 万行）画像 1.8s（命中缓存 0.02s），LLM 叙述 122.7s。

## 端到端链路

```
配置MySQL连接 / 导入文件（CSV·TSV·JSON·XLSX）
  → 测试连通 → 激活 → 元数据（库/表/结构/采样/分布）
  → 自然语言提问 →【第一层：意图确认卡片】→ 生成SQL + 语法/安全校验
  →【第二层：SQL预览确认（结构化分行展示，可编辑）】→ 只读执行（超时+行数封顶）
  → 统计/洞察/图表推荐 → 渐进渲染：摘要 → 图表 → 明细 → 洞察 → SQL折叠
```

界面按**五个阶段**组织（解析意图 / 生成 SQL / 人工确认 / 执行查询 / 分析输出）：
右侧悬浮节点面板可一键收起，点击节点聚焦该阶段；每阶段保留多次尝试（‹ 第 n/m 次 › 切换回看），
重跑某阶段会自动作废其后各阶段的结果。视觉遵循 **Data Dense** 数据密集规范
（详见 `web/README.md`）。

双层确认**后端强制**：`execute_sql` 对未经 `confirm_sql` 确认的会话返回 409，
且必须带 `session_id`，不接受"裸 SQL 直投"。

## 架构

```
basic/
├── main.py                     # FastAPI 入口（basic.main:app）；lifespan 初始化 MySQL 会话存储
├── config/settings.py          # env 驱动配置：LLM/MySQL/超时/LIMIT/缓存/黑名单
├── models/pydantic_schemas.py  # 全部出入参与状态模型
├── api/
│   ├── datasource_api.py       # 数据源增删改查/测试/切换/文件导入
│   ├── meta_api.py             # 元数据：库/表/结构/采样/分布
│   ├── agent_api.py            # 五步链路 + 会话/重命名/移动 + source_insight + sql_advice
│   ├── project_api.py          # 项目（会话分组，每个项目一个本地文件夹）
│   ├── task_api.py             # 任务清单：CRUD / 调序 / 批量执行（确认门 + 只读安全门）
│   └── provider_api.py         # 模型供应商接入（草稿测试/启用禁用/模型清单/激活）
├── core/
│   ├── data_source/            # DataSourceAdapter 抽象 + MySQL + SQLite 文件实现
│   ├── meta_tools.py           # MetaTool：有界元数据探查（索引/行数/列注释）+ 内容画像事实层
│   ├── llm.py                  # LLM 客户端：模型配置解析 + chat/chat_json/chat_text/probe
│   ├── llm_intent.py           # 意图解析（OpenAI 兼容 + 规则兜底）
│   ├── sql_advisor.py          # SQL 优化规则引擎（AST + 元数据，附依据）
│   ├── source_insight.py       # 数据源系统性描述（规则 + LLM 叙述）
│   ├── sql_generator.py        # SQL 校验 + Schema 约束
│   ├── query_executor.py       # 只读执行沙箱：二次校验/超时/脱敏
│   ├── stats_analyzer.py       # 纯 Python 统计
│   ├── insight_engine.py       # 规则洞察：异常/趋势/对比，附依据
│   └── viz_recommender.py      # 图表推荐 + ECharts option
├── security/sql_security.py    # AST 安全门：SELECT-only/单语句/禁写/LIMIT
├── storage/
│   ├── local_store.py          # SQLite：数据源配置
│   └── mysql_store.py          # MySQL：项目 + 会话 + 供应商 + 模型 + 任务清单（含建库建表与老库迁移）
├── scripts/                    # migrate_sessions_to_mysql / verify_new_features
├── utils/                      # helpers（脱敏/类型推断）/ logging / cache
└── web/                        # React 19 + Vite + TS + ECharts 工作台
    └── src/
        ├── api/                # 接口清单 + 与后端 1:1 的类型
        ├── lib/                # sqlFormat（SQL 子句级排版）
        ├── session/            # machine.ts（五阶段状态机）+ RoundProvider（阶段编排）
        ├── state/              # AppProvider（数据源/历史/模型/UI 开关）
        ├── hooks/              # useAsync / useSchema
        ├── components/
        │   ├── stages/         # StagePanel（单阶段聚焦）+ StageBodies（各阶段产出与编辑）
        │   ├── advisor/        # SourceInsightPanel / SqlAdvicePanel / AdviceItem
        │   └── 其余：结果/表格/图表/数据源/库表/模型/历史/回看/确认框
        └── styles/             # tokens · base · layout · components（四层）
```

## 可观测性与运行配置

| 环境变量 | 默认 | 说明 |
|---|---|---|
| `AGENT_MYSQL_HOST/PORT/USER/PASSWORD/DATABASE` | 127.0.0.1/3306/root/''/`data_agent` | 元数据库（会话+模型）；密码缺省复用激活数据源凭据 |
| `AGENT_MYSQL_ENABLED` | `true` | 关掉则会话存储退回 SQLite |
| `LLM_API_BASE` / `LLM_API_KEY` / `LLM_MODEL` | 空 / `qwen-plus` | 环境变量级模型；前端接入的激活模型优先 |
| `LLM_TIMEOUT_S` | `60` | LLM 调用超时；数据源叙述调用只抬不压，下限 `NARRATE_MIN_TIMEOUT_S=200` |
| `LLM_FALLBACK_MODEL` | 空（不启用） | 备用模型 ID：主模型遇可重试错误时自动切过去（同端点同密钥） |
| `PROJECTS_DIR` | `data/projects` | 项目根目录：新建项目不指定路径时在此按项目名建文件夹 |
| `QUERY_TIMEOUT_S` / `DEFAULT_LIMIT` | 10 / 1000 | 查询超时 / 自动追加的 LIMIT 上限 |
| `META_CACHE_TTL_S` | `30` | 表清单与索引缓存秒数，0 = 关闭 |
| `SOURCE_INSIGHT_TTL_S` | `600` | 数据源洞察缓存秒数 |
| `LOG_LEVEL` / `CORS_ORIGINS` | `INFO` / 本机 5173·5174 | 日志级别 / 跨域白名单 |
| `MAX_UPLOAD_MB` / `MAX_IMPORT_ROWS` | 20 / 20 万 | 文件导入上限 |

日志刻意覆盖三件"以前静默发生"的事：**LLM 调用失败降级为规则兜底**、**查询执行失败**、
**超过 1s 的慢请求**；元数据库不可用导致的降级也打 ERROR。日志统一脱敏，
不写连接串/密码/结果数据。

## 安全设计

| 层 | 措施 |
|---|---|
| SQL 生成时 | sqlglot AST：单语句、根仅 SELECT、禁写节点、函数黑名单（load_file 等）、INTO OUTFILE 拦截、LIMIT 强制 |
| SQL 执行时 | 执行器**二次**过安全门 + `SET SESSION max_execution_time` + 行数封顶（多取一行判截断） |
| 人工确认 | 意图确认 → SQL 确认，两层都落在后端会话状态，未确认 409；执行必须带 `session_id` |
| 手工改 SQL | 可执行改写版，但仍强制 `sql_confirmed` + 二次安全校验；执行成功后回写会话以便溯源 |
| 账号 | 建议单独创建仅 SELECT 权限的 MySQL 账号（运维层最后防线） |
| 脱敏 | 异常统一去除连接串/IP/栈信息后才返回前端 |

## 已知边界

- 元数据库（MySQL）不可用时退回 SQLite，会话仍能正常记录（启动日志有 ERROR 告警）。
  降级期间**没有项目分组与重命名**：`/api/agent/*` 的相关分支返回 400 并提示"元数据库降级为 SQLite"，
  `/api/projects`、`/api/providers`、`/api/tasks` 返回 503 并给出排查指引（这几张表只存在于 MySQL，
  不假装成功再丢数据）；带 `session_id` 的提问不做空会话绑定，直接新建记录。
  元数据库密码取自"任意一个 MySQL 数据源"而非仅激活源 —— 否则上传一个 CSV（激活源变成 file）
  就会在下次重启时静默降级（见 `storage/mysql_store.py:_mysql_credential_source`）。
- 删项目时会话移回「未分组」，但**任务清单随项目一起删**：待办是围绕这个项目的数据提出来的，
  挪到未分组只会变成没人认领的噪音。
- 「数据源洞察」分两段取：**事实层**（元数据 + 按列聚合 + 规则建议）实测 5 张表 1.8 秒，
  默认只返回它，所以第一步不再干等大模型；**业务化叙述**（LLM）实测 120~155 秒，改成面板后台
  单独取一次（`narrate=true`，独立缓存键），期间事实与结论照常可读可跳转下一步。
  叙述的超时下限封顶在 `NARRATE_MIN_TIMEOUT_S = 200`，主备模型都抬到同一水位 —— 原先它跟着
  `LLM_TIMEOUT_S=60`，一次必超时、失败后又在备用模型上重试一次，才是那 120 秒假死的原因。
- 「确认添加」不强制要求先测试通过（需求流程是"测试成功再保存"，但端点临时抖动时
  强制拦截会让人无法保存已知可用的配置）。测试结果原样展示，由用户判断。
- LLM 解读文字可能数字失真——结论数值以表格 / KPI / 证据为准。
- 优化建议的行数为 InnoDB 估算值（`information_schema.TABLES.TABLE_ROWS`），只用于分档判断。
- 文件源（SQLite）无 `EXPLAIN`，优化建议以索引/类型/结构层面为主。
