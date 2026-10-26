"""新增能力的本地验证脚本（一次性工具，不入测试套件，因为依赖真实 MySQL）。

覆盖：MySQL 会话读写、模型接入 CRUD、SQL 优化规则、数据源洞察（规则层）。
用法：python -m basic.scripts.verify_new_features
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[2]
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from basic.config.settings import settings                      # noqa: E402
from basic.core.llm import resolve_model                        # noqa: E402
from basic.core.sql_advisor import TableMeta, advise_sql        # noqa: E402
from basic.storage.local_store import LocalStore                # noqa: E402
from basic.storage.mysql_store import MysqlStore, resolve_meta_mysql  # noqa: E402

OK, FAIL = "[OK]", "[XX]"      # 不用 ✓/✗：Windows GBK 控制台编码会崩
results: list[tuple[bool, str]] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    results.append((cond, name))
    print(f"{OK if cond else FAIL} {name}" + (f"  [{extra}]" if extra else ""))


def main() -> int:
    local = LocalStore(settings.store_path)
    cfg = resolve_meta_mysql(local, settings)
    print(f"目标 MySQL：{cfg['user']}@{cfg['host']}:{cfg['port']}/{cfg['database']}\n")
    store = MysqlStore(**cfg, projects_dir=settings.projects_dir)

    # 清理上次运行的残留：脚本要能反复执行，否则唯一键会挡住第二次
    for p in store.list_providers():
        if (p.get("api_base") or "").startswith("https://api.example.com"):
            store.delete_provider(p["id"])
    for p in store.list_projects():
        if p["name"].startswith("验证项目"):
            store.delete_project(p["id"])

    # ------------------------------------------------ 0. 项目
    print("== 项目 ==")
    projects = store.list_projects()
    check("默认「未分组」存在", any(p["is_default"] for p in projects))
    # 用临时目录做项目文件夹：删项目不删文件夹（设计如此），
    # 若直接建在 data/projects 下，每跑一次验证就会在真实目录里留一个空文件夹。
    tmp_root = tempfile.mkdtemp(prefix="verify-proj-")
    pj = store.create_project(name="验证项目A", folder_path=tmp_root)
    check("新建项目自动创建文件夹",
          bool(store.get_project(pj)["folder_path"])
          and os.path.isdir(store.get_project(pj)["folder_path"]))
    check("指定路径时沿用该文件夹",
          os.path.abspath(store.get_project(pj)["folder_path"])
          == os.path.abspath(tmp_root))
    store.update_project(pj, name="验证项目B")
    check("项目可改名", store.get_project(pj)["name"] == "验证项目B")
    check("「未分组」不可删", not store.delete_project(
        next(p["id"] for p in store.list_projects() if p["is_default"])))

    # ------------------------------------------------ 1. 会话读写
    print("\n== 会话 ==")
    sid = store.create_session(ds_id=1, user_question="", project_id=pj)
    check("新建空会话", bool(sid))
    got = store.get_session(sid)
    check("空会话问题为空", got is not None and got["user_question"] == "")
    check("会话归属指定项目", got["project_id"] == pj)
    check("绑定问题到空会话", store.bind_question(sid, ds_id=1, user_question="验证用问题"))
    got = store.get_session(sid)
    check("绑定后问题写入", got["user_question"] == "验证用问题")
    check("绑定自动生成标题", got["title"].startswith("验证用问题"))
    check("已提问会话不可再绑定", not store.bind_question(sid, ds_id=1, user_question="篡改"))
    check("会话可重命名", store.rename_session(sid, "我的自定义标题")
          and store.get_session(sid)["title"] == "我的自定义标题")
    ungrouped = next(p["id"] for p in store.list_projects() if p["is_default"])
    check("会话可移动到别的项目", store.move_session(sid, ungrouped)
          and store.get_session(sid)["project_id"] == ungrouped)
    check("移动到不存在的项目被拒", not store.move_session(sid, 999999))
    check("按项目筛选会话", any(r["session_id"] == sid for r in
                                store.list_rounds(limit=50, project_id=ungrouped)))
    store.update_session(sid, intent="汇总统计", intent_confirmed=True,
                         query_result={"columns": ["a"], "rows": [], "row_count": 0,
                                       "cost_ms": 1, "truncated": False})
    got = store.get_session(sid)
    check("update 写入意图与确认态",
          got["intent"] == "汇总统计" and got["intent_confirmed"] is True)
    check("query_result 存的是合法 JSON",
          isinstance(got["query_result"], dict) and got["query_result"]["cost_ms"] == 1)
    rounds = store.list_rounds(limit=5)
    check("轮次列表含新会话", any(r["session_id"] == sid for r in rounds))
    mine = [r for r in rounds if r["session_id"] == sid][0]
    check("列表按 updated_at 倒序", rounds == sorted(
        rounds, key=lambda r: r["updated_at"], reverse=True))
    check("删除会话", store.delete_round(sid))
    check("删除后读不到", store.get_session(sid) is None)
    check("重复删除返回 False", not store.delete_round(sid))

    # ------------------------------------------------ 2. 供应商与模型
    print("\n== 供应商与模型 ==")
    check("初始可列出供应商", isinstance(store.list_providers(), list))
    pid = store.create_provider(provider_key="verify-demo", name="验证供应商",
                                api_base="https://api.example.com/v1",
                                api_key="sk-test-not-real",
                                protocol="/chat/completions")
    check("创建供应商", bool(pid))
    p = store.get_provider(pid)
    check("api_key 不外泄", "sk-test" not in str(p) and p["has_key"] is True)
    check("协议默认值正确", p["protocol"] == "/chat/completions")
    check("内部出口可取到密钥", bool(store.provider_secret(pid)["api_key"]))

    m1 = store.add_provider_model(pid, "demo-model", make_default=True)
    m2 = store.add_provider_model(pid, "other-model")
    p = store.get_provider(pid)
    check("供应商可挂多个模型", len(p["models"]) == 2)
    check("首个模型成为默认",
          [m["model_name"] for m in p["models"] if m["is_default"]] == ["demo-model"])

    store.set_active_provider_model(m2)
    act = store.active_provider_model()
    check("切换当前模型生效", act["model_id"] == m2 and act["model_name"] == "other-model")
    check("当前模型全局唯一",
          sum(1 for pr in store.list_providers()
              for m in pr["models"] if m["is_active"]) == 1)
    check("当前模型带密钥（内部出口）", bool(act["api_key"]))
    cfg2 = resolve_model(store, settings)
    check("resolve_model 走供应商+模型",
          cfg2.model == "other-model" and cfg2.ready
          and cfg2.url == "https://api.example.com/v1/chat/completions",
          cfg2.url)

    store.update_provider(pid, name="改名后")
    check("供应商可改名", store.get_provider(pid)["name"] == "改名后")
    store.update_provider(pid, api_key="")          # 空串 = 保持原密钥
    check("空密钥不改动原密钥", store.get_provider(pid)["has_key"] is True)
    store.update_provider(pid, enabled=False)
    check("可禁用供应商", store.get_provider(pid)["enabled"] is False)
    check("禁用的供应商不参与 resolve_model",
          resolve_model(store, settings).api_base != "https://api.example.com/v1")
    store.update_provider(pid, enabled=True)
    store.set_active_provider_model(m2)

    check("删除模型", store.delete_provider_model(m1))
    check("删除供应商连带模型", store.delete_provider(pid)
          and store.get_provider(pid) is None)
    check("清理验证项目", store.delete_project(pj))
    act = store.active_provider_model()
    check("当前激活（生产配置）", True,
          f"{act['provider_key']}/{act['model_name']}" if act else "无")

    # ------------------------------------------------ 3. SQL 优化规则
    print("\n== SQL 优化规则 ==")
    meta = TableMeta(
        name="sec_financial_facts",
        columns=[{"name": "filed", "type": "date"}, {"name": "value", "type": "double"},
                 {"name": "taxonomy", "type": "text"}],
        indexes=[],                      # 该表现状就是无任何索引
        row_estimate=25135)
    tables = {"sec_financial_facts": meta}
    sql = ("SELECT * FROM `kaggle_demo`.`sec_financial_facts` "
           "WHERE YEAR(filed) = 2025 AND taxonomy = 'us-gaap' "
           "ORDER BY value")
    advices, err = advise_sql(sql, tables)
    codes = {a.code for a in advices}
    check("解析无错误", err is None, str(err))
    check("识别 SELECT *", "select_star" in codes)
    check("识别缺 LIMIT", "missing_limit" in codes)
    check("识别函数包裹列", "function_on_indexed_column" not in codes)   # 无索引时不报这条
    check("识别大表无 WHERE 全表扫描不误报（本句有 WHERE）",
          "full_scan_no_where" not in codes)
    check("识别无索引表", "table_no_index" in {a.code for a in
          __import__("basic.core.sql_advisor", fromlist=["advise_table"])
          .advise_table(meta)})

    meta2 = TableMeta(name="t2",
                      columns=[{"name": "id", "type": "varchar(32)"},
                               {"name": "amount", "type": "double"}],
                      indexes=[{"name": "PRIMARY", "columns": ["id"], "unique": True,
                                "primary": True}],
                      row_estimate=500000)
    advices2, _ = advise_sql("SELECT id, amount FROM t2 WHERE id = 123 AND amount > 0",
                             {"t2": meta2})
    check("识别隐式类型转换", "implicit_cast" in {a.code for a in advices2})
    advices3, _ = advise_sql("SELECT COUNT(*) FROM t2", {"t2": meta2})
    check("识别无 WHERE 全表扫描（大表）",
          "full_scan_no_where" in {a.code for a in advices3})
    advices4, _ = advise_sql("SELECT id FROM t2 WHERE name LIKE '%abc'", {"t2": meta2})
    check("识别 LIKE 前缀通配", "leading_wildcard_like" in {a.code for a in advices4})
    check("坏 SQL 不抛异常", advise_sql("SELECT FROM WHERE", {})[1] is not None)

    # ------------------------------------------------ 4. 数据源洞察（规则层）
    print("\n== 数据源洞察 ==")
    model = resolve_model(store, settings)
    print(f"  模型：{model.label}（ready={model.ready}）")
    ds = local.get_active()
    if ds:
        from basic.core.data_source.base import build_adapter
        from basic.core.meta_tools import MetaTool
        from basic.core.source_insight import build_profile
        with MetaTool(build_adapter(ds), settings, cache_scope="verify") as tool:
            payload = build_profile(tool=tool, db=ds["database"], tables=[], model=model)
        check("剖析到表", payload["table_count"] > 0, f"{payload['table_count']} 张")
        check("给出数据组织建议", len(payload["advices"]) > 0,
              f"{len(payload['advices'])} 条")
        check("overview 非空", bool(payload["overview"].strip()))
        if payload["llm_used"]:
            check("LLM 增强生效", True)
        else:
            check("无 LLM 时回落规则摘要", "规则引擎" in payload["overview"])
        print("  overview：", payload["overview"][:120].replace("\n", " "))
        print("  首条建议：", payload["advices"][0]["title"] if payload["advices"] else "无")

    store.close()
    local.close()

    print("\n================ 结果 ================")
    bad = [n for ok, n in results if not ok]
    print(f"通过 {len(results) - len(bad)}/{len(results)}")
    for n in bad:
        print(f"  失败：{n}")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
