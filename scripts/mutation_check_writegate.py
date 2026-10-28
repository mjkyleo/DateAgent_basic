"""反向验证：把 writeGate.ts 的关键判定一条条改坏，看测试是不是真的会红。
   跑完必须原样还原，最后再跑一次确认全绿。"""
import pathlib
import re
import shutil
import subprocess
import sys

WEB = pathlib.Path(__file__).resolve().parent.parent / "web"
GATE = WEB / "src" / "lib" / "writeGate.ts"
BAK = GATE.with_suffix(".ts.bak")

MUTATIONS = [
    ("源上限：文件源不再压回只读",
     "if (!ds || ds.type === 'file') return 'read'",
     "if (!ds) return 'read'"),
    ("有效档位：请求档位不再被上限压低",
     "return rankOf(requested) <= rankOf(ceiling) ? requested : ceiling",
     "return requested"),
    ("档位选项：超过上限的档位不再禁选",
     "const overCeiling = rankOf(tier) > rankOf(ceiling)",
     "const overCeiling = false"),
    ("总闸：关着也不再把生效档位压回只读",
     "if (writesEnabled === false) return 'read'",
     "if (false && writesEnabled === false) return 'read'"),
    ("档位选项：总闸关着也不再禁选高档位",
     "const gateOff = writesEnabled === false && tier !== 'read'",
     "const gateOff = false"),
    ("总闸：writesEnabled=false 不再报红",
     "if (writesEnabled === false) {",
     "if (false && writesEnabled === false) {"),
    ("确认：破坏性操作不再要求手输表名",
     "if (plan.destructive && typedPhrase.trim() !== plan.confirm_phrase) {",
     "if (false) {"),
    ("确认：勾选确认位不再是前置条件",
     "if (!ack) blockers.push('需要勾选「我已看清影响面」')",
     "if (!ack) blockers.length && 0"),
    ("上限：预估超上限不再拦",
     "if (plan.estimated_rows !== null && plan.max_allowed_rows > 0",
     "if (false && plan.estimated_rows !== null && plan.max_allowed_rows > 0"),
    ("过期：改过 SQL 之后预览不再作废",
     "return !(same(previewed.sql, cur.sql)",
     "return !(true || same(previewed.sql, cur.sql)"),
    ("备份表名：不再写成\"形如\"，把预览里的名字当成执行时真会用的",
     "return name.replace(/_\\d{8}_\\d{6}_[0-9a-f]{6}$/i, '_…')",
     "return name"),
    ("回滚提示：INSERT 与 CREATE 混成一句话",
     "if (kind === 'create') return",
     "if (kind === 'create' || kind === 'insert') return"),
]


def run_tests():
    p = subprocess.run(["npm", "test"], cwd=WEB, capture_output=True, text=True,
                       shell=True, encoding="utf-8", errors="replace")
    out = (p.stdout or "") + (p.stderr or "")
    m_pass = re.search(r"^ℹ pass (\d+)", out, re.M)
    m_fail = re.search(r"^ℹ fail (\d+)", out, re.M)
    return int(m_pass.group(1) if m_pass else -1), int(m_fail.group(1) if m_fail else -1), out


def main():
    shutil.copyfile(GATE, BAK)
    try:
        p, f, _ = run_tests()
        print(f"基线：pass={p} fail={f}")
        if f != 0 or p <= 0:
            print("基线就不绿，先修好再谈反向验证"); return 1
        bad = []
        for name, old, new in MUTATIONS:
            src = BAK.read_text(encoding="utf-8")
            if old not in src:
                bad.append((name, "改不动：原文没找到")); continue
            GATE.write_text(src.replace(old, new, 1), encoding="utf-8")
            p, f, out = run_tests()
            tag = "红" if f > 0 else "仍绿 ← 测试没钉住"
            print(f"[{tag}] {name} → pass={p} fail={f}")
            if f == 0:
                names = re.findall(r"^✖ (.+?) \(", out, re.M)
                bad.append((name, "仍绿（无失败用例）" + ("：" + "、".join(names) if names else "")))
            GATE.write_text(src, encoding="utf-8")
        p, f, _ = run_tests()
        print(f"还原后：pass={p} fail={f}")
        if f != 0:
            print("还原后仍有失败，需要检查"); return 1
        if bad:
            print("没能钉住的改动：")
            for name, why in bad:
                print(f"  - {name}：{why}")
            return 1
        print(f"全部 {len(MUTATIONS)} 条改动都被测试钉住，还原后全绿。")
        return 0
    finally:
        if BAK.exists():
            shutil.copyfile(BAK, GATE)
            BAK.unlink()


if __name__ == "__main__":
    sys.exit(main())
