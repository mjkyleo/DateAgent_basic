"""MySQL 持久化：会话记录与模型配置（设计文档 §5 的 MySQL 落地）。

与 SQLite 版（local_store.py）的分工：
- **本模块**存"会增长的业务数据"——每一轮问数会话、每一个接入的大模型配置、
  每个项目的待办任务清单；
- local_store 继续存"本地运维配置"——数据源连接（其中文件源指向本机 .db 路径，
  本质上就是本地信息，搬去 MySQL 反而要多配一套连接）。

MySQL 选型理由：会话量随使用增长（一轮含上千行结果 JSON），需要真正的
索引/JSON 类型/并发写；SQLite 的 WAL 在多 worker 下会 "database is locked"。

安全与工程约定：
- 所有**值**走绑定参数（text() + :name）；所有**标识符**经 _q() 包反引号 ——
  MySQL 8.0 里 sql/model 等是保留字，裸写列名会直接 1064（踩过）；
- api_key 只写不读（列表只回 has_key 标记）；
- 建库用 CREATE DATABASE IF NOT EXISTS，重复启动幂等；
- 连接池 pre_ping + recycle，长连接失效自动重连。
"""
from __future__ import annotations

import json
import os
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from ..utils.logging import get_logger

log = get_logger("basic.storage.mysql")


def _now() -> datetime:
    """DATETIME 列用 naive datetime（MySQL DATETIME 无时区），秒级精度足够。"""
    return datetime.now().replace(microsecond=0)


def _iso(v: Any) -> str:
    """读出的 DATETIME → ISO 字符串，与 SQLite 版输出形状保持一致（前端不用改）。"""
    if isinstance(v, datetime):
        return v.isoformat(timespec="seconds")
    return str(v) if v else ""


def _load_json(v: Any) -> Any:
    """JSON 列读出 → Python 对象。

    pymysql 已经把 JSON 列回成字符串，但 SQLAlchemy 侧偶尔直接给 dict；
    解不开就当空（结果列写坏不该让整条记录读不出来）。
    """
    if not v:
        return None
    if isinstance(v, (dict, list)):
        return v
    try:
        return json.loads(v)
    except (json.JSONDecodeError, TypeError):
        return None


def _q(name: str) -> str:
    """标识符包反引号（内部使用，name 一律来自代码常量，不是用户输入）。"""
    return "`" + str(name).replace("`", "") + "`"


#: 对外暴露给一次性工具（迁移脚本等）——它们也要拼列名，
#: 共用同一个引用函数，避免"存储层加了反引号、脚本忘了"再次踩 1064。
quote_ident = _q


class MysqlStore:
    """会话 + 模型配置的 MySQL 存储。接口与 LocalStore 的对应方法同名同签名。"""

    def __init__(self, *, host: str, port: int, user: str, password: str,
                 database: str, connect_timeout: int = 5,
                 projects_dir: str = "") -> None:
        self.database = database or "data_agent"
        # 项目的默认根目录（新建项目不指定路径时，在其下按项目名建文件夹）。
        # 空串 = 不做文件夹相关操作（测试用内存/临时库时无需真实目录）。
        self.projects_dir = projects_dir
        #: 未分组项目的固定名字与目录名
        self.default_project_name = "未分组"
        # 先置 None：ensure_schema 连不上时 __init__ 会抛异常，而调用方
        # （main._init_session_store）还会调 ping() 做二次确认——不预置会让 ping
        # 抛 AttributeError，把真正的"连不上"掩盖成"代码写错了"。
        self._engine: Optional[Engine] = None
        # SQLAlchemy 的 engine 是懒连接：这里只建对象不握手，真正连接发生在
        # ensure_schema 里。所以可以先建"指向目标库"的引擎，再由 ensure_schema
        # 先用 ddl_engine 把库建出来，然后用它建表。
        self._ddl_engine = self._server_engine(host, port, user, password, connect_timeout)
        self._engine = self._db_engine(host, port, user, password, connect_timeout)
        self.ensure_schema()

    # ------------------------------------------------------------ 连接
    def _url(self, host: str, port: int, user: str, password: str, db: str) -> str:
        from urllib.parse import quote_plus
        auth = f"{quote_plus(user)}:{quote_plus(password)}" if user else ""
        return f"mysql+pymysql://{auth}@{host}:{int(port)}/{db}?charset=utf8mb4"

    def _server_engine(self, host: str, port: int, user: str, password: str,
                       timeout: int) -> Engine:
        """连到 mysql 系统库：建库 DDL 需要一个已存在的默认库。"""
        return create_engine(self._url(host, port, user, password, "mysql"),
                             pool_pre_ping=True, pool_recycle=1800,
                             connect_args={"connect_timeout": timeout})

    def _db_engine(self, host: str, port: int, user: str, password: str,
                   timeout: int) -> Engine:
        return create_engine(self._url(host, port, user, password, self.database),
                             pool_pre_ping=True, pool_recycle=1800,
                             connect_args={"connect_timeout": timeout})

    # ------------------------------------------------------------ 建库建表
    def ensure_schema(self) -> None:
        """幂等初始化：建库 + 各表 + 索引。

        刻意不用迁移框架：表少且全是 CREATE IF NOT EXISTS，
        升级时加列走 _ensure_columns 的探测补列（与 SQLite 版同一思路）。
        注意列名一律反引号包裹——`sql`/`model` 在 MySQL 8.0 是保留字。
        """
        db = _q(self.database)
        with self._ddl_engine.begin() as conn:
            conn.execute(text(
                f"CREATE DATABASE IF NOT EXISTS {db} "
                "CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"))
        with self._engine.begin() as conn:
            # 项目：只存"标识 + 本地文件夹路径"，会话正文仍在本表（见 sessions.project_id）。
            # 文件夹是该项目的产物目录，删项目不会删用户的文件夹（只解除关联）。
            conn.execute(text(f"""
                CREATE TABLE IF NOT EXISTS projects (
                    {_q('id')} INT AUTO_INCREMENT PRIMARY KEY,
                    {_q('name')} VARCHAR(128) NOT NULL,
                    -- utf8mb4 下唯一键上限 3072 字节，768*4=3072 正好占满；
                    -- 再长就只能加前缀索引，而路径超过 768 字符本就超 Windows 常规上限
                    {_q('folder_path')} VARCHAR(768) NOT NULL,
                    {_q('created_at')} DATETIME NOT NULL,
                    {_q('updated_at')} DATETIME NOT NULL,
                    UNIQUE KEY uk_project_folder ({_q('folder_path')})
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """))
            conn.execute(text(f"""
                CREATE TABLE IF NOT EXISTS sessions (
                    {_q('session_id')} VARCHAR(32) PRIMARY KEY,
                    {_q('ds_id')} INT NOT NULL DEFAULT 0,
                    {_q('project_id')} INT NOT NULL DEFAULT 0,
                    {_q('title')} VARCHAR(255) NOT NULL DEFAULT '',
                    {_q('user_question')} TEXT,
                    {_q('intent')} VARCHAR(255) NOT NULL DEFAULT '',
                    {_q('target_db')} VARCHAR(128) NOT NULL DEFAULT '',
                    {_q('intent_confirmed')} TINYINT(1) NOT NULL DEFAULT 0,
                    {_q('sql')} MEDIUMTEXT,
                    {_q('sql_confirmed')} TINYINT(1) NOT NULL DEFAULT 0,
                    {_q('query_result')} JSON NULL,
                    {_q('analysis_result')} JSON NULL,
                    {_q('model_name')} VARCHAR(64) NOT NULL DEFAULT '',
                    {_q('created_at')} DATETIME NOT NULL,
                    {_q('updated_at')} DATETIME NOT NULL,
                    KEY idx_sessions_updated ({_q('updated_at')}),
                    KEY idx_sessions_ds ({_q('ds_id')}),
                    KEY idx_sessions_project ({_q('project_id')})
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """))
            # 供应商：一家厂商 = 一个 provider_key + 根地址 + 协议路径 + 密钥
            conn.execute(text(f"""
                CREATE TABLE IF NOT EXISTS llm_providers (
                    {_q('id')} INT AUTO_INCREMENT PRIMARY KEY,
                    {_q('provider_key')} VARCHAR(64) NOT NULL,
                    {_q('name')} VARCHAR(128) NOT NULL,
                    {_q('api_base')} VARCHAR(255) NOT NULL,
                    {_q('protocol')} VARCHAR(64) NOT NULL DEFAULT '/chat/completions',
                    {_q('api_key')} VARCHAR(255) NOT NULL DEFAULT '',
                    {_q('enabled')} TINYINT(1) NOT NULL DEFAULT 1,
                    {_q('created_at')} DATETIME NOT NULL,
                    {_q('updated_at')} DATETIME NOT NULL,
                    UNIQUE KEY uk_provider_key ({_q('provider_key')})
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """))
            # 供应商下的模型清单（1:N）。is_active 全局唯一 = 当前 Agent 使用的模型。
            conn.execute(text(f"""
                CREATE TABLE IF NOT EXISTS llm_provider_models (
                    {_q('id')} INT AUTO_INCREMENT PRIMARY KEY,
                    {_q('provider_id')} INT NOT NULL,
                    {_q('model_name')} VARCHAR(128) NOT NULL,
                    {_q('is_default')} TINYINT(1) NOT NULL DEFAULT 0,
                    {_q('is_active')} TINYINT(1) NOT NULL DEFAULT 0,
                    {_q('created_at')} DATETIME NOT NULL,
                    UNIQUE KEY uk_provider_model ({_q('provider_id')},{_q('model_name')}),
                    KEY idx_pm_provider ({_q('provider_id')})
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """))
            # 任务清单：把分析给出的建议变成人可编辑、可调序、可批量执行的待办。
            # 按项目一份（与 sessions 同归属），所以只按 project_id 过滤 + sort_order 排序。
            # source_ref 存"证据快照"（哪张表、哪条规则、原文数字），
            # 建议本身会随数据变化重算，快照能让任务长期可解释。
            conn.execute(text(f"""
                CREATE TABLE IF NOT EXISTS project_tasks (
                    {_q('id')} INT AUTO_INCREMENT PRIMARY KEY,
                    {_q('project_id')} INT NOT NULL,
                    {_q('ds_id')} INT NOT NULL DEFAULT 0,
                    {_q('title')} VARCHAR(512) NOT NULL,
                    {_q('note')} TEXT,
                    {_q('source_kind')} VARCHAR(32) NOT NULL DEFAULT 'manual',
                    {_q('source_ref')} JSON NULL,
                    {_q('question')} TEXT,
                    {_q('sql')} MEDIUMTEXT,
                    {_q('status')} VARCHAR(16) NOT NULL DEFAULT 'todo',
                    {_q('sort_order')} INT NOT NULL DEFAULT 0,
                    {_q('last_result')} JSON NULL,
                    {_q('session_id')} VARCHAR(32) NOT NULL DEFAULT '',
                    {_q('created_at')} DATETIME NOT NULL,
                    {_q('updated_at')} DATETIME NOT NULL,
                    KEY idx_tasks_project ({_q('project_id')},{_q('sort_order')}),
                    KEY idx_tasks_session ({_q('session_id')})
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """))
            # 写操作审计：每一次写请求（含被拒的）都要留一行，否则"谁删了这张表"
            # 无从追问。被拒的也记，是因为拒绝理由本身就是安全证据 —— 有人反复试探
            # 越权档位时，只有 rejected 行能看出来。
            # 连接凭据不在这张表里（只存 ds_id/ds_name），错误文本由上层先脱敏。
            conn.execute(text(f"""
                CREATE TABLE IF NOT EXISTS sql_audit (
                    {_q('id')} BIGINT AUTO_INCREMENT PRIMARY KEY,
                    {_q('session_id')} VARCHAR(32) NOT NULL DEFAULT '',
                    {_q('ds_id')} INT NOT NULL DEFAULT 0,
                    {_q('ds_name')} VARCHAR(128) NOT NULL DEFAULT '',
                    {_q('target_db')} VARCHAR(128) NOT NULL DEFAULT '',
                    {_q('statement_kind')} VARCHAR(16) NOT NULL DEFAULT '',
                    {_q('sql')} MEDIUMTEXT,
                    {_q('requested_tier')} VARCHAR(16) NOT NULL DEFAULT '',
                    {_q('effective_tier')} VARCHAR(16) NOT NULL DEFAULT '',
                    {_q('confirmed')} TINYINT(1) NOT NULL DEFAULT 0,
                    {_q('destructive')} TINYINT(1) NOT NULL DEFAULT 0,
                    {_q('estimated_rows')} INT NULL,
                    {_q('affected_rows')} INT NULL,
                    {_q('backup_table')} VARCHAR(64) NOT NULL DEFAULT '',
                    {_q('rollback_sql')} MEDIUMTEXT,
                    {_q('status')} VARCHAR(16) NOT NULL DEFAULT 'executed',
                    {_q('error')} TEXT,
                    {_q('cost_ms')} INT NOT NULL DEFAULT 0,
                    {_q('created_at')} DATETIME NOT NULL,
                    KEY idx_audit_created ({_q('created_at')}),
                    KEY idx_audit_session ({_q('session_id')}),
                    KEY idx_audit_ds ({_q('ds_id')},{_q('created_at')})
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """))
        self._ensure_columns()
        self._migrate_legacy_models()
        self._ensure_default_project()
        log.info("MySQL 存储就绪：%s", self.database)

    def _ensure_columns(self) -> None:
        """老库补列（无 ALTER IF NOT EXISTS，按列探测）。"""
        wanted = {
            "sessions": (("model_name", "VARCHAR(64) NOT NULL DEFAULT ''"),
                         ("project_id", "INT NOT NULL DEFAULT 0"),
                         ("title", "VARCHAR(255) NOT NULL DEFAULT ''")),
            "llm_models": (),
        }
        for table, cols in wanted.items():
            if not self._table_exists(table):
                continue
            existing = self._columns_of(table)
            for col, ddl in cols:
                if col not in existing:
                    with self._engine.begin() as conn:
                        conn.execute(text(
                            f"ALTER TABLE {_q(table)} ADD COLUMN {_q(col)} {ddl}"))
                    log.info("补列 %s.%s", table, col)

    def _table_exists(self, table: str) -> bool:
        with self._engine.begin() as conn:
            row = conn.execute(text(
                "SELECT 1 FROM information_schema.TABLES "
                "WHERE TABLE_SCHEMA = :db AND TABLE_NAME = :t"),
                {"db": self.database, "t": table}).fetchone()
        return row is not None

    # ------------------------------------------------------------ 老数据迁移
    def _migrate_legacy_models(self) -> None:
        """把旧的 llm_models（一条=一个供应商+一个模型）搬成 供应商 + 模型 两张表。

        只在「新表还空着、老表有数据」时执行一次。做完把老表改名成
        `llm_models_legacy` 而不是删除：迁移逻辑万一有偏差，配置还能捞回来。
        """
        if not self._table_exists("llm_models"):
            return
        with self._engine.begin() as conn:
            legacy = conn.execute(text(
                f"SELECT {_q('name')},{_q('api_base')},{_q('api_key')},"
                f"{_q('model')},{_q('is_active')} FROM llm_models")).fetchall()
            has_new = conn.execute(
                text("SELECT COUNT(*) FROM llm_providers")).fetchone()[0]
        if not legacy or int(has_new or 0) > 0:
            return
        for idx, (name, api_base, api_key, model, is_active) in enumerate(legacy, start=1):
            base = (api_base or "").rstrip("/")
            key = _provider_key_from(base, idx)
            pid = self.create_provider(provider_key=key, name=name or key,
                                       api_base=base, api_key=api_key or "")
            mid = self.add_provider_model(pid, model, make_default=True)
            if is_active:
                self.set_active_provider_model(mid)
        with self._engine.begin() as conn:
            conn.execute(text("RENAME TABLE llm_models TO llm_models_legacy"))
        log.info("已迁移 %d 条旧模型配置到供应商表（原表改名为 llm_models_legacy）",
                 len(legacy))

    def _ensure_default_project(self) -> None:
        """保证「未分组」项目存在，并把还没归属的会话挂过去。

        让 project_id 永远指向一条真实记录，上层就不用到处判空。
        """
        pid = self.ensure_project(self.default_project_name)
        with self._engine.begin() as conn:
            cur = conn.execute(text(
                f"UPDATE sessions SET {_q('project_id')}=:pid "
                f"WHERE {_q('project_id')}=0 OR {_q('project_id')} IS NULL"),
                {"pid": pid})
            if cur.rowcount:
                log.info("把 %d 条历史会话归入「%s」", cur.rowcount,
                         self.default_project_name)
            # 标题回填：加 title 列之前的老会话标题为空，用问题前 40 字补上，
            # 之后用户在界面上还能改名覆盖。幂等 —— 只动仍为空的行。
            back = conn.execute(text(
                f"UPDATE sessions SET {_q('title')}=LEFT({_q('user_question')},40) "
                f"WHERE ({_q('title')} IS NULL OR {_q('title')}='') "
                f"AND {_q('user_question')} IS NOT NULL AND {_q('user_question')}<>''"))
            if back.rowcount:
                log.info("为 %d 条历史会话补了默认标题", back.rowcount)

    def _columns_of(self, table: str) -> set:
        with self._engine.begin() as conn:
            rows = conn.execute(text(
                "SELECT COLUMN_NAME FROM information_schema.COLUMNS "
                "WHERE TABLE_SCHEMA = :db AND TABLE_NAME = :t"),
                {"db": self.database, "t": table}).fetchall()
        return {r[0] for r in rows}

    def ping(self) -> bool:
        if self._engine is None:
            return False
        try:
            with self._engine.begin() as conn:
                conn.execute(text("SELECT 1"))
            return True
        except Exception as e:  # noqa: BLE001  探活失败只用于降级决策
            log.warning("MySQL 探活失败：%s", type(e).__name__)
            return False

    # ============================================================ 会话
    def create_session(self, *, ds_id: int, user_question: str,
                       intent: str = "", target_db: str = "",
                       project_id: int = 0, title: str = "") -> str:
        """新建会话。user_question 可为空 —— 「新建会话」先落一条空记录，
        之后提问时再绑定（见 bind_question）。"""
        sid = uuid.uuid4().hex[:12]
        now = _now()
        # 带问题新建（非"先建空会话再绑定"那条路）时要顺手生成标题，
        # 否则历史列表里一堆没有标题的行，只能靠问题原文兜底（踩过）。
        title = (title or "").strip() or _auto_title(user_question)
        with self._engine.begin() as conn:
            conn.execute(text(
                f"INSERT INTO sessions({_q('session_id')},{_q('ds_id')},"
                f"{_q('project_id')},{_q('title')},"
                f"{_q('user_question')},{_q('intent')},{_q('target_db')},"
                f"{_q('created_at')},{_q('updated_at')}) "
                f"VALUES(:sid,:ds,:pid,:title,:q,:intent,:db,:now,:now)"),
                {"sid": sid, "ds": ds_id, "pid": int(project_id or 0),
                 "title": title, "q": user_question, "intent": intent,
                 "db": target_db, "now": now})
        return sid

    def bind_question(self, session_id: str, *, ds_id: int, user_question: str) -> bool:
        """把问题写进一条已存在的空会话（新建会话后首次提问走这里）。

        只有 user_question 为空的会话才允许绑定：已问过问题的会话不能被改写，
        否则历史记录会被悄悄替换。标题若为空则用问题前 40 字自动生成。
        """
        with self._engine.begin() as conn:
            cur = conn.execute(text(
                f"UPDATE sessions SET {_q('ds_id')}=:ds, {_q('user_question')}=:q, "
                f"{_q('title')}=CASE WHEN {_q('title')} IS NULL OR {_q('title')}='' "
                f"THEN :title ELSE {_q('title')} END, "
                f"{_q('updated_at')}=:now WHERE {_q('session_id')}=:sid "
                f"AND ({_q('user_question')} IS NULL OR {_q('user_question')}='')"),
                {"sid": session_id, "ds": ds_id, "q": user_question,
                 "title": _auto_title(user_question), "now": _now()})
        return cur.rowcount > 0

    #: 会话列清单（标识符必须逐个包反引号：sql 等是 MySQL 8.0 保留字）
    _SESSION_COLS = ",".join(quote_ident(c) for c in (
        "session_id", "ds_id", "project_id", "title", "user_question",
        "intent", "target_db",
        "intent_confirmed", "sql", "sql_confirmed", "query_result",
        "analysis_result", "model_name", "created_at", "updated_at"))

    def get_session(self, session_id: str) -> Optional[Dict[str, Any]]:
        with self._engine.begin() as conn:
            row = conn.execute(text(
                f"SELECT {self._SESSION_COLS} FROM sessions "
                f"WHERE {_q('session_id')}=:sid"),
                {"sid": session_id}).fetchone()
        return self._row_to_session(row) if row else None

    #: update_session 允许写入的字段（与 SQLite 版一致，外加 model_name/项目/标题）
    _SESSION_WRITABLE = {"ds_id", "project_id", "title", "user_question",
                         "intent", "target_db", "intent_confirmed", "sql",
                         "sql_confirmed", "query_result", "analysis_result",
                         "model_name"}

    def rename_session(self, session_id: str, title: str) -> bool:
        """重命名会话（用户自己起的名字，不受 40 字自动标题限制）。"""
        with self._engine.begin() as conn:
            cur = conn.execute(text(
                f"UPDATE sessions SET {_q('title')}=:t, {_q('updated_at')}=:now "
                f"WHERE {_q('session_id')}=:sid"),
                {"sid": session_id, "t": (title or "").strip()[:255], "now": _now()})
        return cur.rowcount > 0

    def move_session(self, session_id: str, project_id: int) -> bool:
        """把会话移到另一个项目。目标项目必须存在，否则会把会话挂到孤儿 id 上。"""
        with self._engine.begin() as conn:
            if not conn.execute(text(f"SELECT 1 FROM projects WHERE {_q('id')}=:p"),
                                {"p": project_id}).fetchone():
                return False
            cur = conn.execute(text(
                f"UPDATE sessions SET {_q('project_id')}=:p, {_q('updated_at')}=:now "
                f"WHERE {_q('session_id')}=:sid"),
                {"sid": session_id, "p": project_id, "now": _now()})
        return cur.rowcount > 0

    def update_session(self, session_id: str, **fields: Any) -> None:
        sets: Dict[str, Any] = {}
        for k, v in fields.items():
            if k not in self._SESSION_WRITABLE or v is None:
                continue
            if k in ("query_result", "analysis_result"):
                v = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False,
                                                            default=str)
            if k in ("intent_confirmed", "sql_confirmed"):
                v = 1 if v else 0
            sets[k] = v
        if not sets:
            return
        sets["updated_at"] = _now()
        sql = "UPDATE sessions SET " + ", ".join(f"{_q(k)}=:{k}" for k in sets) + \
              f" WHERE {_q('session_id')}=:sid"
        params = dict(sets)
        params["sid"] = session_id
        with self._engine.begin() as conn:
            conn.execute(text(sql), params)

    @staticmethod
    def _row_to_session(row: tuple) -> Dict[str, Any]:
        return {"session_id": row[0], "ds_id": row[1],
                "project_id": int(row[2] or 0), "title": row[3] or "",
                "user_question": row[4] or "",
                "intent": row[5] or "", "target_db": row[6] or "",
                "intent_confirmed": bool(row[7]), "sql": row[8],
                "sql_confirmed": bool(row[9]), "query_result": _load_json(row[10]),
                "analysis_result": _load_json(row[11]), "model_name": row[12] or "",
                "created_at": _iso(row[13]), "updated_at": _iso(row[14])}

    # ------------------------------------------------------------ 轮次历史
    def list_rounds(self, limit: int = 20, ds_id: Optional[int] = None,
                    project_id: Optional[int] = None) -> List[Dict[str, Any]]:
        """历史轮次摘要（倒序）。标量走 JSON_EXTRACT，不反序列化整段结果。"""
        sql = (f"SELECT {_q('session_id')},{_q('ds_id')},{_q('project_id')},"
               f"{_q('title')},{_q('user_question')},"
               f"{_q('intent')},{_q('target_db')},{_q('sql')},"
               f"JSON_UNQUOTE(JSON_EXTRACT({_q('query_result')},'$.row_count')),"
               f"JSON_UNQUOTE(JSON_EXTRACT({_q('query_result')},'$.cost_ms')),"
               f"JSON_UNQUOTE(JSON_EXTRACT({_q('analysis_result')},'$.suggest_chart')),"
               f"JSON_UNQUOTE(JSON_EXTRACT({_q('analysis_result')},'$.summary_text')),"
               f"{_q('query_result')} IS NOT NULL,{_q('created_at')},{_q('updated_at')} "
               "FROM sessions")
        where: List[str] = []
        params: Dict[str, Any] = {}
        if ds_id:
            where.append(f"{_q('ds_id')}=:ds")
            params["ds"] = ds_id
        if project_id:
            where.append(f"{_q('project_id')}=:pid")
            params["pid"] = project_id
        if where:
            sql += " WHERE " + " AND ".join(where)
        # 同秒完成的轮次用 session_id 兜底定序，保证列表顺序稳定
        sql += f" ORDER BY {_q('updated_at')} DESC, {_q('session_id')} DESC LIMIT :lim"
        params["lim"] = max(1, min(int(limit), 500))
        with self._engine.begin() as conn:
            rows = conn.execute(text(sql), params).fetchall()
        return [{
            "session_id": r[0], "ds_id": r[1], "project_id": int(r[2] or 0),
            "title": r[3] or "", "user_question": r[4] or "",
            "intent": r[5] or "", "target_db": r[6] or "", "sql": r[7],
            "row_count": _int_or_none(r[8]),
            "cost_ms": _int_or_none(r[9]),
            "suggest_chart": r[10] or "table",
            "summary_text": r[11] or "",
            "has_result": bool(r[12]),
            "created_at": _iso(r[13]), "updated_at": _iso(r[14]),
        } for r in rows]

    def delete_round(self, session_id: str) -> bool:
        with self._engine.begin() as conn:
            cur = conn.execute(
                text(f"DELETE FROM sessions WHERE {_q('session_id')}=:sid"),
                {"sid": session_id})
        return cur.rowcount > 0

    def delete_rounds_by_ds(self, ds_id: int) -> int:
        """删除数据源时级联清理其会话（与 SQLite 版 delete_datasource 同语义）。"""
        with self._engine.begin() as conn:
            cur = conn.execute(
                text(f"DELETE FROM sessions WHERE {_q('ds_id')}=:ds"), {"ds": ds_id})
        return cur.rowcount

    # ============================================================ 项目
    _PROJECT_COLS = ",".join(quote_ident(c) for c in
                             ("id", "name", "folder_path", "created_at", "updated_at"))

    def ensure_project(self, name: str, folder_path: str = "") -> int:
        """按名字取/建项目，返回 id。用于保证「未分组」一定存在。"""
        with self._engine.begin() as conn:
            row = conn.execute(text(
                f"SELECT {_q('id')} FROM projects WHERE {_q('name')}=:n "
                f"ORDER BY {_q('id')} LIMIT 1"), {"n": name}).fetchone()
        if row:
            return int(row[0])
        return self.create_project(name=name, folder_path=folder_path)

    def create_project(self, *, name: str, folder_path: str = "") -> int:
        """新建项目。folder_path 为空时在 projects_dir 下按项目名建目录。

        目录已存在也允许（用户可能先建好文件夹再挂进来）；
        目录不存在则创建 —— 项目本来就是"一个本地文件夹"，没有目录就没有落点。
        """
        clean_name = (name or "").strip()[:128]
        if not clean_name:
            raise ValueError("项目名不能为空")
        folder = resolve_project_folder(clean_name, folder_path,
                                        getattr(self, "projects_dir", "") or "")
        now = _now()
        with self._engine.begin() as conn:
            cur = conn.execute(text(
                f"INSERT INTO projects({_q('name')},{_q('folder_path')},"
                f"{_q('created_at')},{_q('updated_at')}) VALUES(:n,:f,:now,:now)"),
                {"n": clean_name, "f": folder, "now": now})
            return int(cur.lastrowid or 0)

    def list_projects(self) -> List[Dict[str, Any]]:
        """项目列表（带会话数）。会话数为 0 的项目也要返回，否则新建后看不到。"""
        with self._engine.begin() as conn:
            rows = conn.execute(text(
                f"SELECT p.{_q('id')},p.{_q('name')},p.{_q('folder_path')},"
                f"p.{_q('created_at')},p.{_q('updated_at')},"
                f"(SELECT COUNT(*) FROM sessions s WHERE s.{_q('project_id')}=p.{_q('id')}) "
                f"FROM projects p ORDER BY p.{_q('id')}")).fetchall()
        return [{"id": int(r[0]), "name": r[1], "folder_path": r[2],
                 "created_at": _iso(r[3]), "updated_at": _iso(r[4]),
                 "session_count": int(r[5] or 0),
                 "is_default": r[1] == self.default_project_name} for r in rows]

    def get_project(self, project_id: int) -> Optional[Dict[str, Any]]:
        with self._engine.begin() as conn:
            row = conn.execute(text(
                f"SELECT {self._PROJECT_COLS} FROM projects WHERE {_q('id')}=:id"),
                {"id": project_id}).fetchone()
        if not row:
            return None
        return {"id": int(row[0]), "name": row[1], "folder_path": row[2],
                "created_at": _iso(row[3]), "updated_at": _iso(row[4]),
                "is_default": row[1] == self.default_project_name}

    def update_project(self, project_id: int, **fields: Any) -> Optional[Dict[str, Any]]:
        sets: Dict[str, Any] = {}
        if fields.get("name") is not None:
            clean = str(fields["name"]).strip()[:128]
            if not clean:
                raise ValueError("项目名不能为空")
            sets["name"] = clean
        if fields.get("folder_path") is not None:
            sets["folder_path"] = resolve_project_folder(
                sets.get("name") or (self.get_project(project_id) or {}).get("name", ""),
                str(fields["folder_path"]), getattr(self, "projects_dir", "") or "")
        if sets:
            sets["updated_at"] = _now()
            sql = "UPDATE projects SET " + ", ".join(f"{_q(k)}=:{k}" for k in sets) + \
                  f" WHERE {_q('id')}=:id"
            params = dict(sets)
            params["id"] = project_id
            with self._engine.begin() as conn:
                conn.execute(text(sql), params)
        return self.get_project(project_id)

    def delete_project(self, project_id: int) -> bool:
        """删项目：会话移回「未分组」，任务清单连带删除，不动任何会话与文件。

        默认项目（未分组）不允许删除，否则会话没有落点。
        任务为什么跟着删而不是挪走：清单是"这个项目要办的事"，
        项目没了还留在未分组里只会变成没人认领的噪声；会话是可回看的历史，任务不是。
        """
        proj = self.get_project(project_id)
        if not proj or proj.get("is_default"):
            return False
        fallback = self.ensure_project(self.default_project_name)
        with self._engine.begin() as conn:
            conn.execute(text(
                f"UPDATE sessions SET {_q('project_id')}=:p WHERE {_q('project_id')}=:old"),
                {"p": fallback, "old": project_id})
            conn.execute(text(
                f"DELETE FROM project_tasks WHERE {_q('project_id')}=:old"),
                {"old": project_id})
            conn.execute(text(f"DELETE FROM projects WHERE {_q('id')}=:id"),
                         {"id": project_id})
        return True

    def count_sessions(self) -> int:
        with self._engine.begin() as conn:
            return int(conn.execute(text("SELECT COUNT(*) FROM sessions")).fetchone()[0])

    # ============================================================ 任务清单
    # 分析给出的建议 → 人可编辑、可调序、可批量执行的待办。按项目一份。
    #: 读出列顺序与 _row_to_task 的下标绑定，改动必须同步
    _TASK_COLS = ",".join(quote_ident(c) for c in (
        "id", "project_id", "ds_id", "title", "note", "source_kind",
        "source_ref", "question", "sql", "status", "sort_order",
        "last_result", "session_id", "created_at", "updated_at"))

    #: 上层可写字段。project_id 只在建任务时定（清单属于项目，不做跨项目搬运），
    #: id/时间戳由存储层自己管。
    _TASK_WRITABLE = {"ds_id", "title", "note", "source_kind", "source_ref",
                      "question", "sql", "status", "sort_order",
                      "last_result", "session_id"}

    #: 状态只有四种。"没绑 SQL 所以跑不了"不是一种状态 —— 它一直是 todo，
    #: 由批量执行接口明确跳过并给出原因，人再手动标记完成。
    TASK_STATUSES = ("todo", "running", "done", "failed")

    def list_tasks(self, project_id: int) -> List[Dict[str, Any]]:
        """某项目的任务清单，按 sort_order 升序。

        同 sort_order（并发新建、历史遗留）用 id 兜底定序，否则两次请求顺序可能不同。
        """
        with self._engine.begin() as conn:
            rows = conn.execute(text(
                f"SELECT {self._TASK_COLS} FROM project_tasks "
                f"WHERE {_q('project_id')}=:p "
                f"ORDER BY {_q('sort_order')},{_q('id')}"),
                {"p": int(project_id or 0)}).fetchall()
        return [self._row_to_task(r) for r in rows]

    def get_task(self, task_id: int) -> Optional[Dict[str, Any]]:
        with self._engine.begin() as conn:
            row = conn.execute(text(
                f"SELECT {self._TASK_COLS} FROM project_tasks "
                f"WHERE {_q('id')}=:id"), {"id": int(task_id)}).fetchone()
        return self._row_to_task(row) if row else None

    def next_task_order(self, project_id: int) -> int:
        """新任务排在末尾：当前最大 sort_order + 1（空清单从 1 起）。"""
        with self._engine.begin() as conn:
            row = conn.execute(text(
                f"SELECT MAX({_q('sort_order')}) FROM project_tasks "
                f"WHERE {_q('project_id')}=:p"),
                {"p": int(project_id or 0)}).fetchone()
        return int(row[0] or 0) + 1

    def create_task(self, *, project_id: int, title: str, ds_id: int = 0,
                    note: str = "", source_kind: str = "manual",
                    source_ref: Optional[Any] = None, question: str = "",
                    sql: str = "", status: str = "todo",
                    sort_order: Optional[int] = None) -> int:
        clean = (title or "").strip()[:512]
        if not clean:
            raise ValueError("任务标题不能为空")
        if status not in self.TASK_STATUSES:
            raise ValueError(f"未知任务状态：{status}")
        order = (int(sort_order) if sort_order is not None
                 else self.next_task_order(project_id))
        now = _now()
        with self._engine.begin() as conn:
            cur = conn.execute(text(
                f"INSERT INTO project_tasks({_q('project_id')},{_q('ds_id')},"
                f"{_q('title')},{_q('note')},{_q('source_kind')},"
                f"{_q('source_ref')},{_q('question')},{_q('sql')},"
                f"{_q('status')},{_q('sort_order')},"
                f"{_q('created_at')},{_q('updated_at')}) "
                "VALUES(:pid,:ds,:title,:note,:kind,:ref,:q,:sql,:status,"
                ":order,:now,:now)"),
                {"pid": int(project_id or 0), "ds": int(ds_id or 0),
                 "title": clean, "note": note or "", "kind": source_kind or "manual",
                 "ref": self._json_value(source_ref), "q": question or "",
                 "sql": sql or "", "status": status, "order": order, "now": now})
            return int(cur.lastrowid or 0)

    def update_task(self, task_id: int, **fields: Any) -> Optional[Dict[str, Any]]:
        """改任务字段。

        None 一律忽略（与 update_session 同规则）：清空走空串，不传 None ——
        否则调用方漏传一个字段就会把已有内容抹掉。
        """
        sets: Dict[str, Any] = {}
        for key, value in fields.items():
            if key not in self._TASK_WRITABLE or value is None:
                continue
            if key == "title":
                clean = str(value).strip()[:512]
                if not clean:
                    raise ValueError("任务标题不能为空")
                value = clean
            elif key == "status":
                if value not in self.TASK_STATUSES:
                    raise ValueError(f"未知任务状态：{value}")
            elif key in ("source_ref", "last_result"):
                value = self._json_value(value)
            elif key in ("ds_id", "sort_order"):
                value = int(value)
            sets[key] = value
        if sets:
            sets["updated_at"] = _now()
            statement = ("UPDATE project_tasks SET "
                         + ", ".join(f"{_q(k)}=:{k}" for k in sets)
                         + f" WHERE {_q('id')}=:id")
            params = dict(sets)
            params["id"] = int(task_id)
            with self._engine.begin() as conn:
                conn.execute(text(statement), params)
        return self.get_task(task_id)

    def delete_task(self, task_id: int) -> bool:
        with self._engine.begin() as conn:
            cur = conn.execute(text(
                f"DELETE FROM project_tasks WHERE {_q('id')}=:id"),
                {"id": int(task_id)})
        return cur.rowcount > 0

    def reorder_tasks(self, project_id: int, task_ids: List[int]) -> List[Dict[str, Any]]:
        """按给定 id 顺序重排本项目清单（↑↓ 调序后提交整份顺序）。

        两个坑：
        - 传进来的 id 可能属于别的项目（用户在项目间来回切、界面还挂着旧清单），
          跨项目改排序会把别人的任务挪过来 —— 只认本项目现有的 id；
        - 列表里没出现的任务（并发新建的）不能被丢掉或插到中间：
          统一接到已重排的顺序之后，保持它们原本的相对顺序。
        """
        with self._engine.begin() as conn:
            rows = conn.execute(text(
                f"SELECT {_q('id')} FROM project_tasks "
                f"WHERE {_q('project_id')}=:p "
                f"ORDER BY {_q('sort_order')},{_q('id')}"),
                {"p": int(project_id or 0)}).fetchall()
            known = [int(r[0]) for r in rows]
            listed: List[int] = []
            for raw in task_ids or []:
                try:
                    tid = int(raw)
                except (TypeError, ValueError):
                    continue
                if tid in known and tid not in listed:
                    listed.append(tid)
            final = listed + [tid for tid in known if tid not in listed]
            now = _now()
            for idx, tid in enumerate(final, start=1):
                conn.execute(text(
                    f"UPDATE project_tasks SET {_q('sort_order')}=:o,"
                    f"{_q('updated_at')}=:now WHERE {_q('id')}=:id"),
                    {"o": idx, "now": now, "id": tid})
        return self.list_tasks(project_id)

    @staticmethod
    def _json_value(value: Any) -> Optional[str]:
        """JSON 列写入：dict/list 序列化；已序列化的字符串原样传；空串按 NULL。"""
        if value is None:
            return None
        if isinstance(value, str):
            return value or None
        return json.dumps(value, ensure_ascii=False, default=str)

    @classmethod
    def _row_to_task(cls, row: tuple) -> Dict[str, Any]:
        return {"id": int(row[0]), "project_id": int(row[1] or 0),
                "ds_id": int(row[2] or 0), "title": row[3] or "",
                "note": row[4] or "", "source_kind": row[5] or "manual",
                "source_ref": _load_json(row[6]), "question": row[7] or "",
                "sql": row[8] or "", "status": row[9] or "todo",
                "sort_order": int(row[10] or 0),
                "last_result": _load_json(row[11]), "session_id": row[12] or "",
                "created_at": _iso(row[13]), "updated_at": _iso(row[14])}

    # ============================================================ 写操作审计
    _AUDIT_COLS = ",".join(_q(c) for c in (
        "id", "session_id", "ds_id", "ds_name", "target_db", "statement_kind",
        "sql", "requested_tier", "effective_tier", "confirmed", "destructive",
        "estimated_rows", "affected_rows", "backup_table", "rollback_sql",
        "status", "error", "cost_ms", "created_at"))

    #: 三种就够：跑成了 / 安检或权限拒了 / 真跑到引擎那步失败了。
    #: "拒"必须是一等状态，不然审计表看起来全是 executed，看不出有人在试探越权。
    AUDIT_STATUSES = ("executed", "rejected", "failed")

    def add_audit(self, *, sql: str, status: str, ds_id: int = 0,
                  ds_name: str = "", target_db: str = "", session_id: str = "",
                  statement_kind: str = "", requested_tier: str = "",
                  effective_tier: str = "", confirmed: bool = False,
                  destructive: bool = False, estimated_rows: Optional[int] = None,
                  affected_rows: Optional[int] = None, backup_table: str = "",
                  rollback_sql: str = "", error: str = "",
                  cost_ms: int = 0) -> int:
        """写一条审计。调用方（接口层）在成功、拒绝、失败三条路都调它，
        所以这里不再判空、只把长度截住：审计把库写坏是二次事故。
        """
        if status not in self.AUDIT_STATUSES:
            raise ValueError(f"未知审计状态：{status}")
        with self._engine.begin() as conn:
            cur = conn.execute(text(
                f"INSERT INTO sql_audit({_q('session_id')},{_q('ds_id')},"
                f"{_q('ds_name')},{_q('target_db')},{_q('statement_kind')},"
                f"{_q('sql')},{_q('requested_tier')},{_q('effective_tier')},"
                f"{_q('confirmed')},{_q('destructive')},{_q('estimated_rows')},"
                f"{_q('affected_rows')},{_q('backup_table')},{_q('rollback_sql')},"
                f"{_q('status')},{_q('error')},{_q('cost_ms')},{_q('created_at')}) "
                "VALUES(:sid,:ds,:dsn,:db,:kind,:sql,:rt,:et,:cf,:de,:est,:aff,"
                ":bak,:rb,:st,:err,:ms,:now)"),
                {"sid": (session_id or "")[:32], "ds": int(ds_id or 0),
                 "dsn": (ds_name or "")[:128], "db": (target_db or "")[:128],
                 "kind": (statement_kind or "")[:16], "sql": sql or "",
                 "rt": (requested_tier or "")[:16], "et": (effective_tier or "")[:16],
                 "cf": 1 if confirmed else 0, "de": 1 if destructive else 0,
                 "est": None if estimated_rows is None else int(estimated_rows),
                 "aff": None if affected_rows is None else int(affected_rows),
                 "bak": (backup_table or "")[:64], "rb": rollback_sql or "",
                 "st": status, "err": (error or "")[:2000],
                 "ms": int(cost_ms or 0), "now": _now()})
            return int(cur.lastrowid or 0)

    def list_audits(self, *, limit: int = 50, ds_id: Optional[int] = None,
                    session_id: Optional[str] = None,
                    status: Optional[str] = None) -> List[Dict[str, Any]]:
        """最近 N 条，倒序。数据源多时按 ds_id/session_id 收窄。"""
        cond, params = [], {"limit": max(1, min(int(limit or 50), 500))}
        if ds_id is not None:
            cond.append(f"{_q('ds_id')}=:ds")
            params["ds"] = int(ds_id)
        if session_id:
            cond.append(f"{_q('session_id')}=:sid")
            params["sid"] = session_id
        if status:
            cond.append(f"{_q('status')}=:st")
            params["st"] = status
        where = f"WHERE {' AND '.join(cond)}" if cond else ""
        with self._engine.begin() as conn:
            rows = conn.execute(text(
                f"SELECT {self._AUDIT_COLS} FROM sql_audit {where} "
                f"ORDER BY {_q('id')} DESC LIMIT :limit"), params).fetchall()
        return [self._row_to_audit(r) for r in rows]

    @staticmethod
    def _row_to_audit(row: tuple) -> Dict[str, Any]:
        return {"id": int(row[0]), "session_id": row[1] or "",
                "ds_id": int(row[2] or 0), "ds_name": row[3] or "",
                "target_db": row[4] or "", "statement_kind": row[5] or "",
                "sql": row[6] or "", "requested_tier": row[7] or "",
                "effective_tier": row[8] or "", "confirmed": bool(row[9]),
                "destructive": bool(row[10]),
                "estimated_rows": None if row[11] is None else int(row[11]),
                "affected_rows": None if row[12] is None else int(row[12]),
                "backup_table": row[13] or "", "rollback_sql": row[14] or "",
                "status": row[15] or "executed", "error": row[16] or "",
                "cost_ms": int(row[17] or 0), "created_at": _iso(row[18])}

    # ============================================================ 供应商
    _PROVIDER_COLS = ",".join(quote_ident(c) for c in (
        "id", "provider_key", "name", "api_base", "protocol", "api_key",
        "enabled", "created_at", "updated_at"))

    def create_provider(self, *, provider_key: str, name: str, api_base: str,
                        api_key: str = "", protocol: str = "/chat/completions",
                        enabled: bool = True) -> int:
        now = _now()
        with self._engine.begin() as conn:
            cur = conn.execute(text(
                f"INSERT INTO llm_providers({_q('provider_key')},{_q('name')},"
                f"{_q('api_base')},{_q('protocol')},{_q('api_key')},"
                f"{_q('enabled')},{_q('created_at')},{_q('updated_at')}) "
                f"VALUES(:k,:n,:b,:p,:key,:en,:now,:now)"),
                {"k": provider_key, "n": name, "b": (api_base or "").rstrip("/"),
                 "p": protocol or "/chat/completions", "key": api_key or "",
                 "en": 1 if enabled else 0, "now": now})
            return int(cur.lastrowid or 0)

    @staticmethod
    def _row_to_provider(row: tuple, with_key: bool = False) -> Dict[str, Any]:
        out: Dict[str, Any] = {
            "id": int(row[0]), "provider_key": row[1], "name": row[2],
            "api_base": row[3], "protocol": row[4],
            "has_key": bool(row[5]), "enabled": bool(row[6]),
            "created_at": _iso(row[7]), "updated_at": _iso(row[8]),
        }
        if with_key:
            out["api_key"] = row[5] or ""
        return out

    def list_providers(self) -> List[Dict[str, Any]]:
        """供应商列表，每条带自己的模型清单（1:N），一次查询装完。"""
        with self._engine.begin() as conn:
            rows = conn.execute(text(
                f"SELECT {self._PROVIDER_COLS} FROM llm_providers "
                f"ORDER BY {_q('id')}")).fetchall()
            models = conn.execute(text(
                f"SELECT {_q('id')},{_q('provider_id')},{_q('model_name')},"
                f"{_q('is_default')},{_q('is_active')} FROM llm_provider_models "
                f"ORDER BY {_q('provider_id')},{_q('id')}")).fetchall()
        by_provider: Dict[int, List[Dict[str, Any]]] = {}
        for m in models:
            by_provider.setdefault(int(m[1]), []).append({
                "id": int(m[0]), "model_name": m[2],
                "is_default": bool(m[3]), "is_active": bool(m[4])})
        out = []
        for r in rows:
            item = self._row_to_provider(r)
            item["models"] = by_provider.get(item["id"], [])
            out.append(item)
        return out

    def get_provider(self, provider_id: int) -> Optional[Dict[str, Any]]:
        with self._engine.begin() as conn:
            row = conn.execute(text(
                f"SELECT {self._PROVIDER_COLS} FROM llm_providers "
                f"WHERE {_q('id')}=:id"), {"id": provider_id}).fetchone()
            if not row:
                return None
            item = self._row_to_provider(row)
            item["models"] = [dict(id=int(m[0]), model_name=m[1],
                                   is_default=bool(m[2]), is_active=bool(m[3]))
                              for m in conn.execute(text(
                                  f"SELECT {_q('id')},{_q('model_name')},"
                                  f"{_q('is_default')},{_q('is_active')} "
                                  f"FROM llm_provider_models "
                                  f"WHERE {_q('provider_id')}=:p ORDER BY {_q('id')}"),
                                  {"p": provider_id}).fetchall()]
        return item

    def provider_secret(self, provider_id: int) -> Optional[Dict[str, Any]]:
        """含明文密钥的供应商配置（仅服务端内部：调用/连通性测试）。"""
        with self._engine.begin() as conn:
            row = conn.execute(text(
                f"SELECT {self._PROVIDER_COLS} FROM llm_providers "
                f"WHERE {_q('id')}=:id"), {"id": provider_id}).fetchone()
        return self._row_to_provider(row, with_key=True) if row else None

    def update_provider(self, provider_id: int, **fields: Any) -> Optional[Dict[str, Any]]:
        allowed = ("provider_key", "name", "api_base", "protocol",
                   "api_key", "enabled")
        sets = {k: v for k, v in fields.items() if k in allowed and v is not None}
        # api_key 传空串 = 保持原密钥（编辑表单没改密钥时前端传空）
        if "api_key" in sets and sets["api_key"] == "":
            del sets["api_key"]
        if "api_base" in sets:
            sets["api_base"] = str(sets["api_base"]).rstrip("/")
        if "enabled" in sets:
            sets["enabled"] = 1 if sets["enabled"] else 0
        if sets:
            sets["updated_at"] = _now()
            sql = "UPDATE llm_providers SET " + \
                  ", ".join(f"{_q(k)}=:{k}" for k in sets) + \
                  f" WHERE {_q('id')}=:id"
            params = dict(sets)
            params["id"] = provider_id
            with self._engine.begin() as conn:
                conn.execute(text(sql), params)
        return self.get_provider(provider_id)

    def delete_provider(self, provider_id: int) -> bool:
        """删供应商（连带其模型清单）；若删的是当前激活模型，自动顶上别的。"""
        with self._engine.begin() as conn:
            cur = conn.execute(text(
                f"DELETE FROM llm_providers WHERE {_q('id')}=:id"),
                {"id": provider_id})
            if cur.rowcount <= 0:
                return False
            conn.execute(text(
                f"DELETE FROM llm_provider_models WHERE {_q('provider_id')}=:id"),
                {"id": provider_id})
            row = conn.execute(text(
                f"SELECT {_q('id')} FROM llm_provider_models "
                f"ORDER BY {_q('id')} DESC LIMIT 1")).fetchone()
            if row:
                self._activate_model(conn, int(row[0]))
        return True

    # ------------------------------------------------------ 供应商下的模型
    def add_provider_model(self, provider_id: int, model_name: str,
                           make_default: bool = False) -> int:
        now = _now()
        with self._engine.begin() as conn:
            cur = conn.execute(text(
                f"INSERT INTO llm_provider_models({_q('provider_id')},"
                f"{_q('model_name')},{_q('is_default')},{_q('is_active')},"
                f"{_q('created_at')}) VALUES(:p,:m,:d,0,:now)"),
                {"p": provider_id, "m": model_name.strip()[:128],
                 "d": 1 if make_default else 0, "now": now})
            mid = int(cur.lastrowid or 0)
            if make_default:
                self._set_provider_default(conn, provider_id, mid)
        return mid

    @staticmethod
    def _set_provider_default(conn: Any, provider_id: int, model_id: int) -> None:
        conn.execute(text(
            f"UPDATE llm_provider_models SET {_q('is_default')}=0 "
            f"WHERE {_q('provider_id')}=:p"), {"p": provider_id})
        conn.execute(text(
            f"UPDATE llm_provider_models SET {_q('is_default')}=1 "
            f"WHERE {_q('id')}=:id"), {"id": model_id})

    def delete_provider_model(self, model_id: int) -> bool:
        with self._engine.begin() as conn:
            row = conn.execute(text(
                f"SELECT {_q('provider_id')} FROM llm_provider_models "
                f"WHERE {_q('id')}=:id"), {"id": model_id}).fetchone()
            if not row:
                return False
            provider_id = int(row[0])
            conn.execute(text(
                f"DELETE FROM llm_provider_models WHERE {_q('id')}=:id"),
                {"id": model_id})
            # 删掉的是该供应商的默认模型 → 顶上剩下最新的一个
            left = conn.execute(text(
                f"SELECT {_q('id')} FROM llm_provider_models "
                f"WHERE {_q('provider_id')}=:p ORDER BY {_q('id')} DESC LIMIT 1"),
                {"p": provider_id}).fetchone()
            if left:
                self._set_provider_default(conn, provider_id, int(left[0]))
            rest = conn.execute(text(
                f"SELECT {_q('id')} FROM llm_provider_models "
                f"ORDER BY {_q('id')} DESC LIMIT 1")).fetchone()
            if rest:
                self._activate_model(conn, int(rest[0]))
        return True

    def set_active_provider_model(self, model_id: int) -> bool:
        """切换当前 Agent 使用的模型（同时把它设为其供应商的默认模型）。"""
        with self._engine.begin() as conn:
            row = conn.execute(text(
                f"SELECT {_q('provider_id')} FROM llm_provider_models "
                f"WHERE {_q('id')}=:id"), {"id": model_id}).fetchone()
            if not row:
                return False
            self._activate_model(conn, model_id)
            self._set_provider_default(conn, int(row[0]), model_id)
        return True

    @staticmethod
    def _activate_model(conn: Any, model_id: int) -> None:
        conn.execute(text(f"UPDATE llm_provider_models SET {_q('is_active')}=0"))
        conn.execute(text(f"UPDATE llm_provider_models SET {_q('is_active')}=1 "
                          f"WHERE {_q('id')}=:id"), {"id": model_id})

    def active_provider_model(self) -> Optional[Dict[str, Any]]:
        """当前使用的 (供应商, 模型) 联合记录，**含明文密钥**（resolve_model 用）。"""
        with self._engine.begin() as conn:
            row = conn.execute(text(
                f"SELECT p.{_q('id')},p.{_q('provider_key')},p.{_q('name')},"
                f"p.{_q('api_base')},p.{_q('protocol')},p.{_q('api_key')},"
                f"p.{_q('enabled')},m.{_q('id')},m.{_q('model_name')} "
                f"FROM llm_provider_models m "
                f"JOIN llm_providers p ON p.{_q('id')}=m.{_q('provider_id')} "
                f"WHERE m.{_q('is_active')}=1 AND p.{_q('enabled')}=1 "
                f"ORDER BY m.{_q('id')} DESC LIMIT 1")).fetchone()
        if not row:
            return None
        return {"provider_id": int(row[0]), "provider_key": row[1],
                "provider_name": row[2], "api_base": row[3],
                "protocol": row[4], "api_key": row[5] or "",
                "enabled": bool(row[6]),
                "model_id": int(row[7]), "model_name": row[8]}

    def close(self) -> None:
        try:
            if self._engine is not None:
                self._engine.dispose()
        finally:
            self._ddl_engine.dispose()


def _auto_title(question: str, limit: int = 40) -> str:
    """会话自动标题：取问题首行前 limit 字。用户可后续重命名覆盖它。"""
    line = (question or "").strip().splitlines()[0] if (question or "").strip() else ""
    line = " ".join(line.split())
    return line[:limit]


#: 接口地址 host → 供应商 ID 的默认映射（迁移旧配置时用，让 provider_key 可读）
_HOST_KEYS = (
    ("maas.aliyuncs.com", "aliyun"),
    ("dashscope.aliyuncs.com", "qwen"),
    ("api.deepseek.com", "deepseek"),
    ("api.moonshot.cn", "kimi"),
    ("open.bigmodel.cn", "glm"),
    ("api.minimaxi.com", "minimax"),
    ("api.minimax.chat", "minimax"),
    ("api.xiaomi.com", "mimo"),
)


def _provider_key_from(api_base: str, idx: int) -> str:
    """由接口地址推断可读的供应商 ID；认不出来就用 provider-<序号>。"""
    base = (api_base or "").lower()
    for host, key in _HOST_KEYS:
        if host in base:
            # 同 host 可能有多条（不同密钥），加序号避免撞唯一键
            return key if idx == 1 else f"{key}{idx}"
    return f"provider-{idx}"


def resolve_project_folder(name: str, folder_path: str, projects_dir: str) -> str:
    """算出项目要用的绝对目录，并按需创建。

    - 给了 folder_path：绝对化后直接用；不存在就创建（项目必须有落点）。
    - 没给：在 projects_dir/<项目名> 下建。projects_dir 为空（测试场景）则返回空串。
    目录名做最小净化（去掉路径分隔符与 ..），避免项目名把目录带到别处去。
    """
    from pathlib import Path

    raw = (folder_path or "").strip()
    if raw:
        p = Path(raw).expanduser()
        if not p.is_absolute():
            # 相对路径一律相对 projects_dir 解析，避免随启动 cwd 漂移
            p = Path(projects_dir or ".") / p
        p = Path(os.path.abspath(str(p)))
    elif projects_dir:
        safe = "".join(ch for ch in (name or "").strip()
                       if ch not in '/\\:*?"<>|').strip(". ") or "project"
        p = Path(projects_dir) / safe
    else:
        return ""
    try:
        p.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise ValueError(f"项目文件夹无法创建：{e.strerror or e}") from e
    if not p.is_dir():
        raise ValueError("项目路径不是文件夹")
    return str(p)


def _int_or_none(v: Any) -> Optional[int]:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _mysql_credential_source(store: Any) -> Optional[Dict[str, Any]]:
    """挑一个能借到账号密码的 MySQL 数据源：先看激活的，再看任意一个。

    只看激活源会踩这个坑：用户传了个 CSV（激活源变成 file 类型），下一次重启
    元数据库就取不到密码、静默降级 SQLite —— 表现是「模型/项目面板突然 500」，
    而用户做的只是一件完全正常的换数据源操作。元数据库与业务库同一套账号是
    本函数的立论前提，所以任何一个 MySQL 源的凭据都能用。
    """
    def _is_mysql(ds: Optional[Dict[str, Any]]) -> bool:
        return bool(ds) and (ds.get("type") or "mysql").lower() == "mysql" \
            and (ds.get("password") or "")

    try:
        active = store.get_active() if hasattr(store, "get_active") else None
    except Exception:  # noqa: BLE001  取激活源失败不影响用环境变量默认值
        active = None
    if _is_mysql(active):
        return active
    try:
        rows = store.list_datasources() if hasattr(store, "list_datasources") else []
    except Exception:  # noqa: BLE001
        rows = []
    return next((ds for ds in rows if _is_mysql(ds)), None)


def resolve_meta_mysql(store: Any, settings: Any) -> Dict[str, Any]:
    """元数据库连接参数：环境变量优先；未配密码时复用当前激活 MySQL 数据源的凭据。

    为什么允许复用：元数据库与业务库通常在同一台服务器，运维上是同一套账号，
    让用户再填一遍 host/密码只会提高漏配概率。生产环境请用 AGENT_MYSQL_* 显式指定，
    并给一个最小权限账号（只需对 data_agent 库有 CRUD）。
    """
    host = getattr(settings, "mysql_host", "127.0.0.1")
    port = int(getattr(settings, "mysql_port", 3306) or 3306)
    user = getattr(settings, "mysql_user", "root")
    password = getattr(settings, "mysql_password", "") or ""
    database = getattr(settings, "mysql_database", "data_agent") or "data_agent"
    if not password:
        ds = _mysql_credential_source(store)
        if ds:
            host = ds.get("host") or host
            port = int(ds.get("port") or port)
            user = ds.get("user") or user
            password = ds.get("password") or ""
    return {"host": host, "port": port, "user": user,
            "password": password, "database": database}


__all__ = ["MysqlStore", "resolve_meta_mysql", "quote_ident",
           "resolve_project_folder"]
