"""全局配置：环境变量驱动（可选 basic/.env），LLM 可插拔、安全阈值集中管理。

设计原则（对应设计文档 §1.3）：一切配置化——改行为不发版；
密钥只进环境变量/.env（gitignore），不写进代码。
"""
from __future__ import annotations

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def _load_dotenv(path: Path) -> None:
    """极简 .env 加载（key=value，# 注释），仅填充未存在的变量，不覆盖进程环境。"""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        os.environ.setdefault(k.strip(), v.strip())


_load_dotenv(BASE_DIR / ".env")


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "").strip() or default)
    except ValueError:
        return default


def _bool(name: str, default: bool) -> bool:
    raw = (os.environ.get(name, "") or "").strip().lower()
    return default if raw == "" else raw in ("1", "true", "yes", "on")


class Settings:
    """运行期配置快照（进程启动时读取一次）。"""

    def __init__(self) -> None:
        # ---- LLM（OpenAI 兼容端点：通义千问 / OpenAI / DeepSeek 等，切换只改环境变量）----
        self.llm_api_key = os.environ.get("LLM_API_KEY") or os.environ.get("OPENAI_API_KEY", "")
        self.llm_api_base = (os.environ.get("LLM_API_BASE")
                             or os.environ.get("OPENAI_API_BASE", "")).rstrip("/")
        self.llm_model = os.environ.get("LLM_MODEL", "qwen-plus")
        self.llm_timeout_s = _int("LLM_TIMEOUT_S", 60)
        # 备用模型：主模型不可用时自动切换的目标（同端点同密钥，只换模型 ID）。
        # 留空 = 不启用自动切换。要换端点/密钥请用前端「模型管理」激活另一个模型。
        self.llm_fallback_model = (os.environ.get("LLM_FALLBACK_MODEL", "") or "").strip()
        # 无 key 时自动走规则兜底（MVP 可离线演示与测试）
        self.llm_enabled = bool(self.llm_api_key and self.llm_api_base)

        # ---- 查询执行 ----
        self.query_timeout_s = _int("QUERY_TIMEOUT_S", 10)     # MySQL 会话级 max_execution_time
        self.default_limit = _int("DEFAULT_LIMIT", 1000)       # 自动追加的 LIMIT 上限
        self.max_sample_rows = _int("MAX_SAMPLE_ROWS", 10)     # 元数据采样默认行数

        # ---- 存储 ----
        # 相对路径一律解析到 basic/ 目录（避免随启动 cwd 漂移）
        store = os.environ.get("STORE_PATH", "data/store.db")
        self.store_path = str((BASE_DIR / store).resolve() if not os.path.isabs(store) else store)
        # 上传文件（CSV/TSV/JSON/XLSX）解析后落成 SQLite 的目录
        self.uploads_dir = str(BASE_DIR / "data" / "uploads")
        # 项目根目录：新建项目不指定路径时，在其下按项目名建独立文件夹。
        # 会话正文仍存 MySQL，文件夹只做"项目标识 + 产物目录"。
        proj = os.environ.get("PROJECTS_DIR", "data/projects")
        self.projects_dir = str((BASE_DIR / proj).resolve()
                                if not os.path.isabs(proj) else proj)
        self.max_upload_mb = _int("MAX_UPLOAD_MB", 20)
        self.max_import_rows = _int("MAX_IMPORT_ROWS", 200_000)

        # ---- 元数据库（会话记录 + 模型配置存 MySQL，见 storage/mysql_store.py）----
        # 数据源连接仍留 SQLite：其中文件源指向本机 .db 路径，属本地运维配置。
        # 未配 AGENT_MYSQL_PASSWORD 时，启动会尝试复用当前激活 MySQL 数据源的凭据
        # （同一台服务器通常是同一套账号），省去重复配置；生产请显式指定。
        self.mysql_enabled = _bool("AGENT_MYSQL_ENABLED", True)
        self.mysql_host = os.environ.get("AGENT_MYSQL_HOST", "127.0.0.1")
        self.mysql_port = _int("AGENT_MYSQL_PORT", 3306)
        self.mysql_user = os.environ.get("AGENT_MYSQL_USER", "root")
        self.mysql_password = os.environ.get("AGENT_MYSQL_PASSWORD", "")
        self.mysql_database = os.environ.get("AGENT_MYSQL_DATABASE", "data_agent")

        # ---- 服务 ----
        self.host = os.environ.get("HOST", "127.0.0.1")
        self.port = _int("PORT", 8001)
        # 跨域：默认只放通本机 dev server（5174/5173）；生产用 CORS_ORIGINS 显式指定，
        # "*" 通配会带上凭据风险，这里默认不开放。
        self.cors_origins = [o.strip() for o in os.environ.get(
            "CORS_ORIGINS", "http://localhost:5174,http://127.0.0.1:5174,"
                            "http://localhost:5173,http://127.0.0.1:5173").split(",") if o.strip()]
        self.log_level = (os.environ.get("LOG_LEVEL", "INFO") or "INFO").strip().upper()

        # ---- 元数据缓存（秒）：表清单短时间复用，少打几次 information_schema ----
        self.meta_cache_ttl_s = _int("META_CACHE_TTL_S", 30)
        # 数据源洞察剖析结果缓存（秒）：剖析要打十几次元数据查询 + 一次 LLM 调用，
        # 不能每提一次问就重算；前端可传 refresh=true 强制刷新
        self.source_insight_ttl_s = _int("SOURCE_INSIGHT_TTL_S", 600)
        # 查询执行失败/LLM 降级等场景是否记录 WARNING（默认开）
        self.log_degrade = _bool("LOG_DEGRADE", True)

        # ---- 安全黑名单（AST 节点级 + 函数级，见 security/sql_security.py）----
        self.forbidden_statements = {
            "Insert", "Update", "Delete", "Drop", "Create", "Alter",
            "TruncateTable", "Merge", "Grant", "Revoke", "Use", "Set",
        }
        self.forbidden_functions = {
            "load_file", "into_outfile", "into_dumpfile",  # MySQL 文件读写
            "benchmark", "sleep", "pg_sleep",              # 资源耗尽
            "load_extension", "read_csv", "read_parquet",  # 越出数据库边界
        }

    def llm_label(self) -> str:
        return self.llm_model if self.llm_enabled else "规则兜底（未配置 LLM）"


settings = Settings()
