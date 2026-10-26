"""Agent 核心链路接口（设计文档 §7.3/§10.1）。

四步链路 + 后端强制的双层人工确认：
1. POST /parse_intent   —— 自然语言 → 意图确认卡片（不生成最终 SQL 之外的任何动作）；
2. POST /generate_sql   —— 确认意图后生成 SQL 并安全校验（仅预览，不执行）；
3. POST /confirm_sql    —— 用户二次确认（后端落状态，未确认的 SQL 禁止执行）；
4. POST /execute_sql    —— 只读执行（内部二次安全校验 + 超时 + 行数封顶）；
5. POST /analysis       —— 统计 / 洞察 / 图表推荐（纯计算，无 LLM 推理）。

任何一层取消即终止；execute_sql 对未确认会话返回 409，确认不可绕过。

两条增强链路（SQL 优化能力）：
- POST /sql_advice     —— 分析用户编写的 SQL，给出优化方案（规则引擎 + 可选 LLM 解读）；
- POST /source_insight —— 第一阶段的数据源系统性描述与优化建议（规则 + LLM 增强，带缓存）。

存储分工：数据源配置在 SQLite（local_store），会话与模型配置在 MySQL
（mysql_store）。见 storage/ 两个模块的文档字符串。
"""
from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

import sqlglot
from fastapi import APIRouter, HTTPException, Request
from sqlglot import exp

from ..core.llm import ModelConfig, chat_text, resolve_fallback, resolve_model
from ..core.llm_intent import parse_intent as llm_parse_intent
from ..core.meta_tools import MetaTool
from ..core.insight_engine import extract_insights
from ..core.sql_advisor import TableMeta, advise_sql
from ..core.sql_generator import generate_sql
from ..core.source_insight import build_profile
from ..core.stats_analyzer import analyze
from ..core.viz_recommender import build_chart_option, recommend
from ..models.pydantic_schemas import (Advice, AnalysisRequest, AnalysisResult,
                                       ConfirmSqlRequest, ExecuteSqlRequest,
                                       GenerateSqlRequest, IntentConfirmCard,
                                       MoveSessionRequest, NewSessionRequest,
                                       ParseIntentRequest, QueryResult,
                                       RenameSessionRequest,
                                       SourceInsightRequest,
                                       SourceInsightResult, SqlAdviceRequest,
                                       SqlAdviceResult, SqlSecurityResult,
                                       TableFact)
from ..security.sql_security import check_sql_safe
from ..utils.cache import cache_get, cache_set
from ..utils.helpers import sanitize_error
from ..utils.logging import get_logger

log = get_logger("basic.api.agent")
router = APIRouter(prefix="/api/agent", tags=["agent"])


# ---------------------------------------------------------------- 存储与模型
def _ds_store(request: Request):
    """数据源配置（SQLite）。"""
    return request.app.state.store


def _sessions(request: Request):
    """会话与模型配置（MySQL；不可用时已由启动期降级为 SQLite）。"""
    return request.app.state.sessions


def _model(request: Request) -> ModelConfig:
    return resolve_model(_sessions(request), request.app.state.settings)


def _fallback(request: Request):
    """备用模型（LLM_FALLBACK_MODEL）。主模型遇到可重试类错误时自动切过去。"""
    return resolve_fallback(request.app.state.settings)


# ---------------------------------------------------------------- 项目兼容层
# 会话存储可能是 MySQL（有项目概念），也可能是启动期降级的 SQLite（没有）。
# 这层垫片让调用点不用到处判断存储能力，也保证测试注入 LocalStore 时照常跑。
def _has_projects(store: Any) -> bool:
    return hasattr(store, "list_projects")


def _project_of(store: Any, project_id: Optional[int]) -> int:
    """解析目标项目 id：显式指定且存在则用它，否则「未分组」；无项目能力时返回 0。

    "存在"要真的查一下：前端可能记着一个已被删除的项目 id，
    照单全收会把会话挂到孤儿 id 上，之后任何按项目筛选都看不到它。
    """
    if not _has_projects(store):
        return 0
    if project_id and store.get_project(int(project_id)):
        return int(project_id)
    return int(store.ensure_project(store.default_project_name))


def _create_session(store: Any, *, ds_id: int, user_question: str,
                    intent: str = "", target_db: str = "",
                    project_id: int = 0, title: str = "") -> str:
    if _has_projects(store):
        return store.create_session(ds_id=ds_id, user_question=user_question,
                                    intent=intent, target_db=target_db,
                                    project_id=project_id, title=title)
    return store.create_session(ds_id=ds_id, user_question=user_question,
                                intent=intent, target_db=target_db)


def _resolve_ds(request: Request, ds_id: int | None) -> dict:
    store = _ds_store(request)
    ds = store.get_datasource(ds_id) if ds_id else None
    if ds is None:
        ds = store.get_active()
    if not ds:
        raise HTTPException(400, "尚未配置或激活数据源：请先新增 MySQL 连接并切换激活")
    return ds


def _meta_tool(request: Request, ds: dict) -> MetaTool:
    from ..core.data_source.base import build_adapter
    return MetaTool(build_adapter(ds), request.app.state.settings,
                    cache_scope=f"ds{ds.get('id')}")


# ---------------------------------------------------------------- 1. 意图解析
@router.post("/parse_intent", response_model=IntentConfirmCard)
def parse_intent_endpoint(body: ParseIntentRequest, request: Request):
    ds = _resolve_ds(request, body.ds_id)
    with _meta_tool(request, ds) as tool:
        db = body.selected_db or ds.get("database") or ""
        if not db:
            dbs = tool.list_databases()
            if not dbs:
                raise HTTPException(400, "该数据源下没有可用数据库")
            db = dbs[0]
        try:
            schema_context = tool.schema_context(db, body.selected_tables, body.selected_fields)
        except Exception as e:  # noqa: BLE001
            raise HTTPException(500, f"加载 Schema 失败：{sanitize_error(str(e))}") from e
        # 表引用怎么写（`库`.`表` 还是 `表`）由数据源类型决定，一并带给意图解析器：
        # 写错了生成的 SQL 根本执行不了（SQLite 上就是 "no such table: db.table"）。
        qualify = tool.qualified_name
        qualifies = tool.qualifies_tables

    model = _model(request)
    card, _client, model_label = llm_parse_intent(
        question=body.user_query.strip(), schema_context=schema_context,
        selected_db=db, selected_tables=body.selected_tables,
        selected_fields=body.selected_fields, settings=request.app.state.settings,
        qualify=qualify, qualifies=qualifies, model=model,
        fallback=_fallback(request))

    store = _sessions(request)
    question = body.user_query.strip()
    # 会话绑定：前端「新建会话」先落一条空记录，首次提问时复用同一条，
    # 而不是每次提问都新开一条（否则历史里会堆满空会话）。
    sid = ""
    if body.session_id:
        existing = store.get_session(body.session_id)
        if (existing and not (existing.get("user_question") or "").strip()
                and hasattr(store, "bind_question")):
            # SQLite 降级存储没有 bind_question（也无空会话概念）：缺防护会 500，
            # 与 project/rename 垫片同款式的能力判断，走下面的新建分支。
            if not store.bind_question(body.session_id, ds_id=ds["id"],
                                       user_question=question):
                sid = ""        # 绑定失败（并发/已占用）→ 走新建，不报错
            else:
                sid = body.session_id
                store.update_session(sid, intent=card.intent,
                                     target_db=card.target_db, model_name=model_label)
    if not sid:
        sid = _create_session(store, ds_id=ds["id"], user_question=question,
                              intent=card.intent, target_db=card.target_db,
                              project_id=_project_of(store, body.project_id))
        store.update_session(sid, model_name=model_label)
    card.session_id = sid
    return card


# ---------------------------------------------------------------- 2. SQL 生成与校验
@router.post("/generate_sql", response_model=SqlSecurityResult)
def generate_sql_endpoint(body: GenerateSqlRequest, request: Request):
    card = _card_from(body.intent_context)
    raw_sql = card.sql_preview
    if not raw_sql.strip():
        return SqlSecurityResult(passed=False, reason="意图卡片中没有可用的 SQL 预览",
                                 final_sql=None)
    # Schema 约束：以目标库真实表清单为准（防幻觉表）
    # 优先用本轮会话绑定的数据源（用户中途切换激活源也不影响本轮）
    sess = _sessions(request).get_session(body.session_id) if body.session_id else None
    ds = _resolve_ds(request, (sess or {}).get("ds_id"))
    with _meta_tool(request, ds) as tool:
        try:
            known_tables = tool.list_tables(card.target_db)
        except Exception as e:  # noqa: BLE001  取不到表清单时跳过存在性校验（安全门仍生效）
            log.warning("取表清单失败，跳过 Schema 存在性校验 ds=%s：%s",
                        ds.get("id"), sanitize_error(str(e)))
            known_tables = []

    result = generate_sql(raw_sql, {"tables": known_tables},
                          max_limit=request.app.state.settings.default_limit,
                          settings=request.app.state.settings)
    # 走到这一步说明用户已确认意图（第一层确认落库，回看时才不会显示"未确认"）。
    # 与 SQL 是否通过安全门无关——确认的是意图，不是 SQL。
    if body.session_id:
        _sessions(request).update_session(body.session_id, intent_confirmed=True)
    if result.passed and body.session_id:
        _sessions(request).update_session(body.session_id, sql=result.final_sql,
                                          sql_confirmed=False)
    return result


# ---------------------------------------------------------------- 3. SQL 二次确认
@router.post("/confirm_sql")
def confirm_sql_endpoint(body: ConfirmSqlRequest, request: Request):
    session = _sessions(request).get_session(body.session_id)
    if not session:
        raise HTTPException(404, "会话不存在或已过期")
    if not session.get("sql"):
        raise HTTPException(409, "该会话尚未生成 SQL")
    _sessions(request).update_session(body.session_id, sql_confirmed=True)
    return {"success": True, "msg": "SQL 已确认，可以执行"}


# ---------------------------------------------------------------- 4. 只读执行
@router.post("/execute_sql", response_model=QueryResult)
def execute_sql_endpoint(body: ExecuteSqlRequest, request: Request):
    # 双层确认的第二层：**必须**落在会话上，不认"裸 SQL 直投"。
    # 早前 session_id 缺省时会跳过本校验 —— 等于前端换一种调用姿势就能绕过人工确认，
    # 这里把口子堵死：没有会话/会话未确认，一律 409。
    if not body.session_id:
        raise HTTPException(409, "缺少会话标识：SQL 必须经「确认执行」后才能运行")
    session = _sessions(request).get_session(body.session_id)
    if not session:
        raise HTTPException(404, "会话不存在或已过期")
    if not session.get("sql_confirmed"):
        raise HTTPException(409, "SQL 尚未确认：请先在预览面板点击「确认执行」")

    sql = body.sql.strip() or session.get("sql") or ""
    if not sql:
        raise HTTPException(400, "没有可执行的 SQL")
    ds_id = body.ds_id or session.get("ds_id")
    ds = _resolve_ds(request, ds_id)

    try:
        result = request.app.state.executor.execute(ds=ds, sql=sql)
    except PermissionError as e:
        raise HTTPException(403, str(e)) from e
    except Exception as e:  # noqa: BLE001  执行器已脱敏，这里再兜一层
        log.warning("查询执行失败 session=%s：%s", body.session_id, sanitize_error(str(e)))
        raise HTTPException(500, sanitize_error(str(e))) from e

    _sessions(request).update_session(body.session_id,
                                      query_result=result.model_dump(mode="json"))
    # 用户手工改过 SQL 时回写会话：否则回看时"显示的 SQL"与"跑出结果的那条 SQL"
    # 不是同一条，数字无法溯源。这里只能改已执行过的那条（已过安全门），
    # 且不动 sql_confirmed —— 二次确认状态属于本轮流程，与 SQL 文本无关。
    executed = body.sql.strip()
    if executed and executed != (session.get("sql") or ""):
        _sessions(request).update_session(body.session_id, sql=executed)
        log.info("会话 %s 使用了手工修改后的 SQL（已回写以便溯源）", body.session_id)
    return result


# ---------------------------------------------------------------- 5. 统计分析
@router.post("/analysis", response_model=AnalysisResult)
def analysis_endpoint(body: AnalysisRequest, request: Request):
    try:
        qr = QueryResult(**body.query_result)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, f"query_result 结构不合法：{e}") from e

    stats = analyze(qr)
    insights = extract_insights(stats)
    chart = recommend(stats, body.intent)
    option = build_chart_option(chart, qr.model_dump(mode="json"), stats)

    result = AnalysisResult(summary_text=_summary(qr, stats), stats=stats,
                            insights=insights, suggest_chart=chart, chart_option=option)
    if body.session_id:
        _sessions(request).update_session(body.session_id,
                                          analysis_result=result.model_dump(mode="json"))
    return result


# ---------------------------------------------------------------- 会话：新建 / 查询
@router.post("/sessions")
def new_session(body: NewSessionRequest, request: Request):
    """新建一条空会话（无问题），之后首次提问时绑定到它。

    为什么先落库：用户确认的方案就是"新建=落库空会话"——历史里能看见这条记录，
    切走再回来不会丢；已问过问题的会话不会被改写（bind_question 只认空会话）。
    """
    ds = _resolve_ds(request, body.ds_id)
    store = _sessions(request)
    pid = _project_of(store, body.project_id)
    sid = _create_session(store, ds_id=ds["id"], user_question="", project_id=pid)
    return {"session_id": sid, "ds_id": ds["id"], "project_id": pid, "created": True}


@router.get("/session/{session_id}")
def get_session(session_id: str, request: Request):
    session = _sessions(request).get_session(session_id)
    if not session:
        raise HTTPException(404, "会话不存在或已过期")
    return session


# ---------------------------------------------------------------- 轮次历史
@router.get("/rounds")
def list_rounds(request: Request, limit: int = 200, ds_id: Optional[int] = None,
                project_id: Optional[int] = None):
    """历史轮次摘要（倒序）。每一轮 = 一条 session 记录，可按项目筛选。"""
    store = _sessions(request)
    if project_id and _has_projects(store):
        rounds = store.list_rounds(limit=limit, ds_id=ds_id, project_id=project_id)
    else:
        rounds = store.list_rounds(limit=limit, ds_id=ds_id)
    return {"rounds": rounds}


@router.get("/rounds/{session_id}")
def get_round(session_id: str, request: Request):
    """回看某一轮的完整记录（问题/意图/SQL/结果/洞察/图表）。"""
    session = _sessions(request).get_session(session_id)
    if not session:
        raise HTTPException(404, "轮次不存在或已删除")
    return session


@router.delete("/rounds/{session_id}")
def delete_round(session_id: str, request: Request):
    if not _sessions(request).delete_round(session_id):
        raise HTTPException(404, "轮次不存在或已删除")
    return {"success": True, "msg": "已删除该轮记录"}


@router.patch("/rounds/{session_id}")
def rename_round(session_id: str, body: RenameSessionRequest, request: Request):
    """重命名会话。改名不动 user_question —— 那是这一轮的原始提问，要留痕。"""
    store = _sessions(request)
    if not store.get_session(session_id):
        raise HTTPException(404, "轮次不存在或已删除")
    if not hasattr(store, "rename_session"):
        raise HTTPException(400, "当前会话存储不支持重命名（元数据库降级为 SQLite）")
    store.rename_session(session_id, body.title)
    return {"success": True, "session_id": session_id,
            "title": body.title.strip()[:255]}


@router.patch("/rounds/{session_id}/project")
def move_round(session_id: str, body: MoveSessionRequest, request: Request):
    """把会话移到另一个项目。目标项目必须存在，否则会话会挂到不存在的 id 上。"""
    store = _sessions(request)
    if not store.get_session(session_id):
        raise HTTPException(404, "轮次不存在或已删除")
    if not hasattr(store, "move_session"):
        raise HTTPException(400, "当前会话存储不支持项目分组（元数据库降级为 SQLite）")
    if not store.move_session(session_id, body.project_id):
        raise HTTPException(404, "目标项目不存在")
    return {"success": True, "session_id": session_id,
            "project_id": body.project_id}


# ---------------------------------------------------------------- 6. SQL 优化建议
@router.post("/sql_advice", response_model=SqlAdviceResult)
def sql_advice_endpoint(body: SqlAdviceRequest, request: Request):
    """分析一条 SQL（通常是用户手工改过的版本），给出查询优化方案。

    不执行 SQL、不改写会话 —— 只做静态剖析。规则引擎的结论是即时且免费的；
    narrate=True 时才调 LLM 把结论讲成业务语言（按需触发，避免每次编辑都花 token）。
    """
    sql = (body.sql or "").strip().rstrip(";").strip()
    if not sql:
        raise HTTPException(400, "SQL 为空")

    sess = _sessions(request).get_session(body.session_id) if body.session_id else None
    ds = _resolve_ds(request, body.ds_id or (sess or {}).get("ds_id"))

    # 只读性先告知（没过安全门不影响展示建议，但要让用户知道它执行不了）
    gate = check_sql_safe(sql, max_limit=request.app.state.settings.default_limit,
                          settings=request.app.state.settings)
    readable = bool(gate.get("passed"))

    tables_meta, parse_error = _table_metas_for_sql(request, ds, sql)
    advices, sql_parse_error = advise_sql(sql, tables_meta,
                                          max_limit=request.app.state.settings.default_limit)
    parse_error = sql_parse_error
    facts = [TableFact(table=m.name, row_estimate=m.row_estimate,
                       indexes=m.indexes, column_count=len(m.columns))
             for m in tables_meta.values()]

    model = _model(request)
    summary = _rule_summary(advices, tables_meta)
    llm_used = False
    if body.narrate and model.ready and advices:
        try:
            summary = _narrate_sql(sql, advices, tables_meta, model, _fallback(request))
            llm_used = True
        except Exception as e:  # noqa: BLE001  LLM 失败保留规则结论
            log.warning("SQL 优化 LLM 解读失败：%s", type(e).__name__)

    return SqlAdviceResult(sql=sql, readable=readable, parse_error=parse_error,
                           advices=[Advice(**a.as_dict()) for a in advices],
                           facts=facts, summary=summary, model_label=model.label,
                           llm_used=llm_used)


def _table_metas_for_sql(request: Request, ds: dict, sql: str
                         ) -> tuple[Dict[str, TableMeta], Optional[str]]:
    """从 SQL 里取引用到的表，逐张取元数据（结构/索引/行数）。"""
    names: List[str] = []
    try:
        trees = sqlglot.parse(sql, read="mysql")
        for tree in trees:
            if tree is None:
                continue
            for node in tree.find_all(exp.Table):
                if node.name and node.name.lower() not in [n.lower() for n in names]:
                    names.append(node.name)
    except Exception as e:  # noqa: BLE001
        return {}, f"SQL 语法解析失败：{type(e).__name__}"
    if not names:
        return {}, None

    out: Dict[str, TableMeta] = {}
    with _meta_tool(request, ds) as tool:
        db = ds.get("database") or ""
        for name in names[:8]:
            try:
                cols = tool.table_schema(db, name).get("columns") or []
            except Exception as e:  # noqa: BLE001  单表取不到就跳过（不给无依据的建议）
                log.warning("取表结构失败 %s：%s", name, type(e).__name__)
                continue
            from ..core.sql_advisor import ColumnMetaLite
            out[name.lower()] = TableMeta(
                name=name,
                columns=[ColumnMetaLite(name=c.get("name") or "", type=c.get("type") or "",
                                        primary_key=bool(c.get("primary_key")))
                         for c in cols],
                indexes=tool.table_indexes(db, name),
                row_estimate=tool.table_row_estimate(db, name))
    return out, None


_SQL_ADVICE_PROMPT = """你是 SQL 优化顾问。下面是一条 SQL、规则引擎发现的优化问题，
以及相关表的真实元数据（行数/索引）。请用业务语言给出简短解读。

要求：
1. 只依据给定事实，禁止编造表名、字段名或数字；
2. 先讲"最该先改的一件事"及原因，再列其余项，总共不超过 4 条；
3. 说明每项改动对查询速度/资源的影响；
4. 中文，3-6 句话，不要术语堆砌。

SQL：
{sql}

规则引擎发现的问题：
{issues}

表元数据：
{facts}
"""


def _narrate_sql(sql: str, advices: List[Any], tables: Dict[str, TableMeta],
                 model: ModelConfig, fallback: Any = None) -> str:
    issues = "\n".join(f"- [{a.severity}] {a.title}：{a.suggestion}（依据：{a.evidence}）"
                       for a in advices)
    facts = "\n".join(
        f"- {m.name}：{m.size_label()}；索引："
        + ("；".join(f"{i.get('name')}({', '.join(i.get('columns') or [])})"
                     for i in m.indexes) or "无")
        for m in tables.values())
    return chat_text(model, system="你是资深 DBA，擅长把优化建议讲成业务影响。",
                     user=_SQL_ADVICE_PROMPT.format(sql=sql, issues=issues, facts=facts),
                     fallback=fallback)


def _rule_summary(advices: List[Any], tables: Dict[str, TableMeta]) -> str:
    """无 LLM 时的确定性汇总（同样只讲事实）。"""
    if not advices:
        sizes = "；".join(f"{m.name} {m.size_label()}" for m in tables.values())
        return f"未发现明显的查询优化问题。{sizes}" if sizes else "未发现明显的查询优化问题。"
    high = [a for a in advices if a.severity == "high"]
    head = f"发现 {len(advices)} 项可优化点"
    if high:
        head += f"，其中 {len(high)} 项影响较大，建议优先处理：" + \
                "；".join(a.title for a in high[:3])
    return head + "。"


# ---------------------------------------------------------------- 7. 数据源洞察
@router.post("/source_insight", response_model=SourceInsightResult)
def source_insight_endpoint(body: SourceInsightRequest, request: Request):
    """第一阶段：对所选数据源做系统性描述与分析（含查询优化建议）。

    默认（narrate=false）只跑规则层：真实元数据 + 内容画像 + 结构化问题清单，
    1~2 秒可用且每次结果一致。narrate=true 才调 LLM 把事实讲成业务场景（慢，
    前端在后台单独取，用户先看到事实）。结果按 (数据源, 库, 是否叙述) 缓存，
    避免每提一次问就重新剖析一遍（元数据查询 + LLM 调用都不便宜）；
    refresh=true 可强制重算。
    """
    ds = _resolve_ds(request, body.ds_id)
    with _meta_tool(request, ds) as tool:
        db = body.db or ds.get("database") or ""
        if not db:
            dbs = tool.list_databases()
            if not dbs:
                raise HTTPException(400, "该数据源下没有可用数据库")
            db = dbs[0]

        ttl = float(getattr(request.app.state.settings, "source_insight_ttl_s", 600) or 0)
        # 规则层与叙述层分开缓存：默认请求（narrate=false）只要 1~2 秒的事实，
        # 不能因为有人点过一次「AI 解读」，就把两分钟的叙述载荷当成所有人的默认结果；
        # 反过来也不能让叙述层的重算把已经算好的事实再算一遍。
        base = f"srcinsight:ds{ds.get('id')}:{db}"
        key = f"{base}:narr" if body.narrate else base
        if not body.refresh and ttl > 0:
            ok, cached = cache_get(key, ttl)
            if ok:
                cached["from_cache"] = True
                return _insight_result(cached)

        t0 = time.perf_counter()
        payload = build_profile(tool=tool, db=db, tables=body.tables,
                                model=_model(request), fallback=_fallback(request),
                                with_narration=body.narrate)
        payload["ds_id"] = ds.get("id")
        payload["ds_name"] = ds.get("name") or ""
        payload["cost_ms"] = int((time.perf_counter() - t0) * 1000)
        if ttl > 0:
            cache_set(key, payload, ttl)
        return _insight_result(payload)


def _insight_result(p: Dict[str, Any]) -> SourceInsightResult:
    """dict → 响应模型（缓存里存的是 dict，取出时再过一次模型校验）。"""
    return SourceInsightResult(**p)


# ---------------------------------------------------------------- 内部
def _card_from(data: dict) -> IntentConfirmCard:
    try:
        return IntentConfirmCard(**data)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, f"intent_context 结构不合法：{e}") from e


def _summary(qr: QueryResult, stats: dict) -> str:
    """确定性文字摘要（非 LLM 生成，数字可溯源）。"""
    parts = [f"查询返回 {qr.row_count} 行 × {len(qr.columns)} 列，耗时 {qr.cost_ms} ms"]
    if qr.truncated:
        parts.append("（结果触达行数上限已截断，可追加条件缩小范围）")
    group = stats.get("group")
    if group:
        gs = group["groups"]
        if gs:
            parts.append(f"按 {group['category']} 分 {len(gs)} 组，"
                         f"头部「{gs[0]['group']}」{group['value']} 为 {gs[0]['sum']:g}")
    trend = stats.get("trend")
    if trend:
        s = trend["series"]
        parts.append(f"时间序列覆盖 {len(s)} 个月（{s[0]['period']} ~ {s[-1]['period']}）")
    return "；".join(parts) + "。"


__all__ = ["router"]
