# 问数 · 数据分析 Agent —— 前端

只读、双层人工确认的数据分析 Agent 前端。后端为 `basic/`（FastAPI，默认 8001）。

## 启动

```bash
npm install
npm run dev        # http://127.0.0.1:5174，/api 已代理到 8001
npm run build      # tsc --noEmit + vite build
```

后端需先启动（仓库根 `data-agent-lite/`）：

```bash
python -m uvicorn basic.main:app --port 8001
```

## 目录

```
src/
  api/          types.ts（与后端 Pydantic 模型 1:1）· http.ts（fetch 封装/ApiError）· index.ts（接口清单）
  lib/          sqlFormat.ts（SQL 子句级排版）
  hooks/        useAsync（请求三态）· useSchema（库表懒加载）
  state/        AppProvider（数据源/历史/模型/UI 开关）
  session/      machine.ts（五阶段状态机）· RoundProvider（阶段编排）
  components/
    stages/     StagePanel（单阶段聚焦）· StageBodies（各阶段产出与编辑区）
    advisor/    SourceInsightPanel（阶段①数据源洞察）· SqlAdvicePanel（阶段③SQL 建议）· AdviceItem
    tasks/      TaskListPanel（任务清单抽屉：编辑 / ↑↓ 调序 / 核对后批量执行）
    StageNavigator.tsx（右侧悬浮阶段导航）
    CodeBlock.tsx（SqlBlock 排版展示 / SqlEditor 编辑）
    DataTable.tsx（高密度表格：数值列 mono 右对齐 / 状态列彩色徽章）
    ResultView.tsx（KPI + 摘要 + 图表 + 明细 + 洞察 + SQL 溯源）
    HistoryList.tsx（项目分组 + 重命名/移动/删除）
    ProviderPanel.tsx（供应商模板标签 + 表单 + 已接入列表）
    ConfirmDialog.tsx（通用确认模态框）
    其余：图表 / 数据源 / 库表 / 回看 / 输入区 / 顶栏 / 弹层
  styles/       tokens（设计变量）· base（重置+动效）· layout（骨架）· components（组件）
```

## 设计语言：Data Dense（数据密集）

整体遵循 [Data Dense](https://luckymaomi.github.io/ui/styles/data-dense/) 规范，
规则写在 `styles/tokens.css` 顶部注释里，并且尽量落在变量上 —— 组件层想违规也用不出来：

| 规则 | 落地 |
| --- | --- |
| 表格行 ≤ `py-2`、表格文字 `text-xs` | 单元格 `padding: 4px 10px`、`font-size: 12px` |
| 表头 `uppercase text-[10px] tracking-wide` | `.data-table th` 10px 大写 + `letter-spacing: .06em` |
| 数字与 ID 用 `font-mono` | `.num`（右对齐 + `tabular-nums`）/ `.mono` / `.idx`，KPI 值同样等宽 |
| 状态彩色编码 | green=active、yellow=pending、red=error、blue=info；徽章带同色圆点 |
| 按钮 `px-2.5 py-1 text-xs` | `.btn-primary/.btn-ghost` = `4px 10px` / 12px，`active:scale-.97` 反馈 |
| 禁止 `p-6+` / `rounded-xl+` / 渐变 / 重阴影 | 面板内边距 ≤10px、圆角最大 6px、输入区遮罩渐变已移除、阴影只留一档 |
| 移动端表格横向滚动、KPI 堆叠 | `.table-scroll` 内部滚动、`.kpi-row` flex-wrap |
| 常用操作直接可见 | 阶段主动作、图表切换、行数切换全部内联，不藏菜单 |

**表格状态列是自动识别的**（`DataTable.tsx`）：列名像状态、取值 ≤10 种、且多数取值命中状态词表，
三者同时满足才涂色。猜错颜色比不给颜色更糟，所以词表只收语义明确的词
（active/pending/failed、excellent/poor、positive/negative）并做整值精确匹配；
纯数值开关列（0/1）不算状态列 —— 把 `1` 涂成绿色、文字却显示 "1" 反而是噪声。
ID 列与数值列按列名区分：ID 左对齐等宽（标识符不是度量），金额/计数右对齐等宽。

### 动效

取自 Remotion 的 timing 原则（Web 仪表盘用 CSS 实现，不做帧驱动）：
统一缓动 `cubic-bezier(0.16, 1, 0.3, 1)`、交互 150ms / 进场 200ms、只动
`scale/translate/opacity`，并整体尊重 `prefers-reduced-motion`。

- `.animate-in` 区块淡入上移；`.stagger` 列表逐项进场（序号封顶 8 项，避免整列等太久）
- `CountUp`：KPI 数字滚动（rAF + easeOutQuart），减少动效时直接落终值
- `.skeleton` 微光骨架屏；`.flash` 数值更新高亮；`dd-pulse` 进行中状态呼吸
- 图表密集化在 `EChart.tsx` 的 `densify()`：紧凑坐标轴、细虚线网格、规范色板、深色 tooltip

## SQL 优化能力（两条增强链路）

| 端点 | 触发位置 | 产出 |
| --- | --- | --- |
| `POST /api/agent/source_insight` | 阶段①「解析意图」 | 数据源系统性描述 + 数据组织方式改进建议 |
| `POST /api/agent/sql_advice` | 阶段③「人工确认」编辑 SQL 时 | 该条 SQL 的查询优化方案 |

**规则引擎 + LLM 增强**（`core/sql_advisor.py` + `core/source_insight.py`）：

- 规则层是确定性的，同一条 SQL + 同一份元数据 → 同一份结论。硬事实来自两处：
  SQL 结构走 sqlglot AST（与安全门同一个解析器），表元数据走适配器新增的
  `table_indexes()` / `table_row_estimate()`（MySQL 读 information_schema，文件源读 PRAGMA/COUNT）。
- 每条建议都带 `evidence`（依据）—— 没有依据的建议就是猜测，不如不提。
  所以词表只收语义明确的词（active/pending/failed、excellent/poor、positive/negative）
  并做整值精确匹配；纯数值开关列（0/1）不算状态列。
- LLM 只负责"把事实讲成业务语言"，不参与判断；提示词里明确禁止编造表名/字段/数字。
  无 API key 时规则结论完整可用（`overview` 回落为规则摘要）。
- 阶段①面板多出一栏「数据里的现象」：后端把逐列聚合出的真实数字（时间跨度、维度高频值、
  度量范围与均值、空值率）交给模型，每条现象都必须指回其中某个数字；
  无模型时同样由规则层直接摘这些事实，不会硬凑（`phenomena` 为空 = 没取到内容画像）。
- 阶段①洞察面板是**两段式**：先发 `narrate=false` 只取事实层（实测 1.8 秒，概览/现象/表画像/
  结构建议立刻渲染），拿到后**后台自动补一次 `narrate=true`**（实测 122.7 秒）把业务化叙述、
  改进计划、查询建议换进来；期间工具栏显示「业务化叙述生成中…」，失败则给出提示 + 「重试 AI 叙述」，
  并明确说明下面的事实与结论不依赖大模型、照常可用 —— 第一步不该为了等模型而空转两分钟。
  前端超时按两段分开：事实层 60s，叙述 300s（后端对叙述另设 200s 的下限）。
- 规则引擎（阶段③ SQL 建议）保持**编辑停下 debounce 700ms 即时自动跑**，不花 token；
  **AI 解读按需点按钮**，避免每次按键都调模型。
- `source_insight` 结果按 (数据源, 库) 缓存（默认 600s，`SOURCE_INSIGHT_TTL_S`），
  带叙述的那份另用一个 `:narr` 键；前端「重新剖析」传 `refresh=true` 绕过。

## 项目分组与模型供应商

### 项目（会话分组）

左栏按**项目**分组，一个项目对应一个本地文件夹。分组用原生 `<details>`：默认展开、
不额外维护折叠状态、键盘可用。每条会话三个内联操作（重命名 / 移动 / 删除），
删除与删项目都走 `ConfirmDialog`，不用浏览器原生 `confirm()`。

当前项目存在 `localStorage`（`dd.projectId`）：只是 React state 的话刷新后会静默回到
「未分组」，新会话就落到别的项目里去了。项目被删则自动回落。

### 任务清单抽屉

`tasks/TaskListPanel.tsx`：把建议面板里的优化项收进**按项目一份**的待办，可以编辑、调序、
核对后统一执行。三个刻意的取舍：

- **「统一执行」不是点一下就跑**：先把待执行的 SQL 逐条摊开、勾一次「我已核对以上 N 条 SQL」，
  「确认执行」才亮（后端同样强制 `confirmed`，缺了就 409）。
- **没绑 SQL 的任务不静默消失**：默认跑"全部未完成"，这类任务逐条返回跳过原因并提示改「已完成」；
  「加入清单」带过来的建议大多属于这种，能跑的（纯 SELECT 核查）才真的去查。
- **调序用 ↑↓ 而不是拖拽**：不为此引拖拽库；每点一次提交整份 id 顺序，避免逐条 ±1 的并发错位。

任务行上「查看结果」跳到那一轮的只读回看（执行时会留下一条 `intent=任务清单批量执行` 的会话）；
`AdviceItem` 的「加入清单」把当场的证据快照（规则码 / severity / 依据 / 库表）写进 `source_ref`，
按钮随即变「已在清单」，重复点击不会攒出一样的任务。

### 供应商接入面板

`ProviderPanel.tsx`：8 个快捷模板标签 + 6 字段表单 + 已接入列表。

- **模板只回填表单，不直接落库** —— 否则会攒下一堆连不通的配置。
- 「测试连接」用**草稿配置**直接测（`POST /api/providers/test`），不必先保存；
  没填模型名时只验「端点可达 + 密钥有效」，填了才验证该模型真能调用。
- 「接口地址」要含版本前缀，请求 URL = 接口地址 + 接口协议，表单里有提示。
- 密钥输入框 `type=password` + 眼睛切换；编辑时留空 = 保持原密钥。
- 已接入列表支持：启用/禁用（禁用后不参与模型解析）、测试、编辑模型清单、
  点模型 chip 切换为当前使用、删除（确认模态框）。

## 会话存储与模型接入

- **项目、会话、供应商、模型清单都存 MySQL**（`storage/mysql_store.py`，库名 `data_agent`，
  `AGENT_MYSQL_*` 环境变量可改；未配密码时复用激活 MySQL 数据源的凭据）。
  数据源连接仍留 SQLite —— 其中文件源指向本机 `.db` 路径，属本地运维配置。
- MySQL 不可用时启动**显式降级**为 SQLite 并打 ERROR 日志（`/healthz` 可见），
  不会静默把会话写进本地文件；降级期间项目分组与重命名接口明确报错，不假装成功。
- 迁移：`python -m basic.scripts.migrate_sessions_to_mysql [--dry-run]`（幂等，
  已存在的会话不覆盖；SQLite 原库保留作备份）。
  旧的 `llm_models` 表会一次性升成「供应商 + 模型」两张表，原表改名为
  `llm_models_legacy` 留底。
- 「新建会话」落一条空记录，首次提问时 `parse_intent` 带 `session_id` 绑定
  （已问过问题的会话不可被改写）；删除一律走应用内确认模态框，不再用原生 `confirm()`。

## 三个关键设计

**1. 类型是后端模型的镜像。** `api/types.ts` 与 `basic/models/pydantic_schemas.py` 字段同名
（含 `snake_case`），不做"前端友好"改名 —— 改名就要写映射层，映射层是字段对不上的根源。

**2. 阶段 + 尝试（分支），而不是一条平铺的流水线。**

一轮分析 = 五个阶段，每阶段的执行记一次 `attempt`：

| 阶段 | 后端接口 | 可编辑内容 |
| --- | --- | --- |
| ① 解析意图 | `POST /parse_intent` | 意图描述、目标库、目标表、目标字段 |
| ② 生成 SQL | `POST /generate_sql` | —（产出由后端决定） |
| ③ 人工确认 | `POST /confirm_sql` | SQL 语句（改后执行，后端回写会话） |
| ④ 执行查询 | `POST /execute_sql` | — |
| ⑤ 分析输出 | `POST /analysis` | 图表类型、明细展示行数 |

退回某阶段改完再跑会**新增一次尝试**，旧尝试保留可回看（卡片头部「‹ 第 n/m 次 ›」切换），
导航节点显示 ⑂ 分叉标记。重跑会自动把其后所有阶段标为 `stale` —— 改了 SQL 却还留着
旧查询结果是这类界面最容易犯的错，作废逻辑统一放在 reducer 里，不靠调用方自觉。

每次尝试内部仍是 append-only 事件（思考 / 工具调用 / 确认 / 输出 / 异常），
先落 `running`、结束按 key 改 `ok` / `failed`，所以"Agent 这一步在干什么"始终可见。

**3. 链路与后端落库状态严格对应。** 前端不发明流程：

| 前端动作 | 后端接口 | 后端落库 |
| --- | --- | --- |
| 提问 | `POST /api/agent/parse_intent` | 建 session |
| 确认意图 | `POST /api/agent/generate_sql` | `intent_confirmed=True`、写入待确认 SQL |
| 确认执行 | `POST /api/agent/confirm_sql` → `/execute_sql` | `sql_confirmed=True`（未确认执行返回 409） |
| 分析 | `POST /api/agent/analysis` | 存 `analysis_result` |

### SQL 可编辑带来的后端改动

`execute_sql` 的 `ExecuteSqlRequest.sql` 缺省才用会话内的 SQL，因此支持执行手工修改后的
语句；执行前仍强制 `session_id` + `sql_confirmed` + `QueryExecutor.execute` 内的二次安全
校验，**绕过不了确认与安全门**。但后端原本只落 `query_result`、不回写 `session.sql`，
会出现"回看显示原 SQL、结果是编辑版跑出来的"，故在 `api/agent_api.py` 的执行成功后补了一步：
传入自定义 SQL 时同步回写会话，保证数字可溯源。

## 界面分区

- **左栏**：按项目分组的历史对话（一轮一条，点开回看只读明细）；
  每条可重命名 / 移动到其他项目 / 删除；顶部可新建会话与新建项目。
- **右栏 + 右侧悬浮导航**：五个阶段节点常驻页面右上，浮于对话之上、可一键收起为圆点条；
  点击节点切换聚焦，主区一次只展开一个阶段，其余收起为可切换列表项，
  阶段卡片内含过程消息、产出与编辑区、退回与重跑操作。
- **底部输入区**：提问框 + 数据源 / 库表结构 / 任务清单 / 模型供应商弹出面板 + 文件导入。
  任务清单入口带未完成条数角标，清单为空时不显示。

## 约定

- 不引 UI 库与状态库：样式走 CSS 变量，状态用 Context + reducer，图标内联 SVG。
- 所有请求错误统一收敛为 `ApiError`，组件只 catch 一种错误。
- 图表按需注册 ECharts 组件，注册项与 `core/viz_recommender.py` 产出的 option 保持一致
  （少注册一个，控制台会报错且对应部分不渲染）。
