"""数据分析Agent MVP —— FastAPI 应用入口。

启动方式（从仓库根 data-agent-lite/ 执行）：
    python -m uvicorn basic.main:app --port 8001
或：
    python -m basic.main            # 直接用 settings 里的 HOST/PORT
端口默认 8001，避开同机其他分析服务；前端 dev server（basic/web）代理 /api 到此。
"""
from __future__ import annotations

import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from basic.api.agent_api import router as agent_router
from basic.api.datasource_api import router as datasource_router
from basic.api.meta_api import router as meta_router
from basic.api.project_api import router as project_router
from basic.api.provider_api import router as provider_router
from basic.api.task_api import router as task_router
from basic.config.settings import settings
from basic.core.query_executor import QueryExecutor
from basic.storage.local_store import LocalStore
from basic.storage.mysql_store import MysqlStore, resolve_meta_mysql
from basic.utils.helpers import sanitize_error
from basic.utils.logging import get_logger, setup_logging

log = get_logger("basic.main")


def _init_session_store(app: FastAPI):
    """会话存储：MySQL 优先，连不上退回 SQLite（保证服务仍能起来）。

    降级必须显式告警到 ERROR：否则用户以为会话写进了 MySQL，
    实际全落在本地文件里，换台机器/删了文件就"丢会话"。
    """
    st = app.state.settings
    if not getattr(st, "mysql_enabled", True):
        log.warning("AGENT_MYSQL_ENABLED=false：会话存储退回 SQLite（%s）", st.store_path)
        return app.state.store
    try:
        cfg = resolve_meta_mysql(app.state.store, st)
        store = MysqlStore(**cfg, projects_dir=getattr(st, "projects_dir", ""))
        if not store.ping():
            raise RuntimeError("探活失败")
        log.info("会话存储：MySQL %s@%s:%s/%s", cfg["user"], cfg["host"],
                 cfg["port"], cfg["database"])
        return store
    except Exception as e:  # noqa: BLE001  启动不能因元数据库挂了而整体失败
        log.error("MySQL 会话存储不可用（%s），已退回 SQLite；排查：%s",
                  type(e).__name__, sanitize_error(str(e)))
        return app.state.store


@asynccontextmanager
async def lifespan(app: FastAPI):
    """进程级资源的生命周期：启动时建、退出时关。

    之前 store/executor 只建不关 —— 测试里反复 create_app 会漏 SQLite 连接，
    生产重启时 WAL 也可能留下没 checkpoint 的脏页。

    create_app 预初始化过一轮 state（让不进 lifespan 的用法也能跑）；
    这里覆盖前先把它建的旧 store/会话存储关掉，否则那套 SQLite 连接与
    SQLAlchemy 引擎池从未释放，每次启动/重载都泄漏一整套。
    """
    setup_logging(settings.log_level)
    # create_app 预初始化过一轮 state（让不进 lifespan 的用法也能跑）；
    # 覆盖前先把它建的旧 store/会话存储关掉，否则那套 SQLite 连接与
    # SQLAlchemy 引擎池从未释放，每次启动/重载都泄漏一整套。
    _release_state(app)
    app.state.settings = settings
    app.state.store = LocalStore(settings.store_path)
    app.state.sessions = _init_session_store(app)
    app.state.executor = QueryExecutor(settings)
    log.info("服务就绪：store=%s llm=%s", settings.store_path, settings.llm_label())
    try:
        yield
    finally:
        _release_state(app)
        log.info("服务已停止")


def _release_state(app: FastAPI) -> None:
    """关闭 app.state 上的存储资源。

    sessions 降级时就是 store 本身（同一个 LocalStore 实例），重复 close
    会报错且没必要，所以只在两者不是同一对象时关 sessions。
    """
    sessions = getattr(app.state, "sessions", None)
    if sessions is not None and sessions is not getattr(app.state, "store", None):
        try:
            sessions.close()
        except Exception:  # noqa: BLE001  收尾失败不该掩盖原始退出原因
            log.warning("关闭 MySQL 会话存储失败")
    store = getattr(app.state, "store", None)
    if store is not None:
        try:
            store.close()
        except Exception:  # noqa: BLE001
            log.warning("关闭本地存储失败")


def create_app() -> FastAPI:
    setup_logging(settings.log_level)
    app = FastAPI(title="数据分析Agent MVP", version="1.0.0",
                  description="最小可用端到端数据分析Agent：只读、双层人工确认、可解释",
                  lifespan=lifespan)
    # 跨域白名单走 CORS_ORIGINS（默认只放本机 dev server），不再无脑 allow_origins=["*"]
    app.add_middleware(CORSMiddleware, allow_origins=settings.cors_origins,
                       allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
                       allow_headers=["*"])

    # 进程级共享状态（MVP 单实例）；lifespan 里会重新绑定，这里保证
    # 直接实例化（测试/脚本）也能拿到可用的 state。
    app.state.settings = settings
    app.state.store = LocalStore(settings.store_path)
    app.state.sessions = _init_session_store(app)
    app.state.executor = QueryExecutor(settings)

    app.include_router(datasource_router)
    app.include_router(meta_router)
    app.include_router(agent_router)
    app.include_router(project_router)
    app.include_router(provider_router)
    app.include_router(task_router)

    @app.middleware("http")
    async def _access_log(request: Request, call_next):
        """极简访问日志：方法/路径/耗时/状态码（不记 body，避免泄露查询内容）。"""
        t0 = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            log.exception("请求异常 %s %s", request.method, request.url.path)
            raise
        cost = int((time.perf_counter() - t0) * 1000)
        if cost >= 1000:            # 只记慢请求，避免刷屏
            log.info("慢请求 %s %s %sms → %s", request.method, request.url.path,
                     cost, response.status_code)
        return response

    @app.get("/healthz", tags=["ops"])
    def healthz():
        return {"status": "ok", "llm": settings.llm_label(),
                "store": settings.store_path}

    @app.exception_handler(Exception)
    async def unhandled(request: Request, exc: Exception):
        """兜底：任何未处理异常都脱敏后返回 500，不泄露栈/IP/凭据。"""
        log.exception("未处理异常 %s %s", request.method, request.url.path)
        return JSONResponse(status_code=500,
                            content={"detail": f"服务内部错误：{sanitize_error(str(exc))}"})

    return app


app = create_app()

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("basic.main:app", host=settings.host, port=settings.port)
