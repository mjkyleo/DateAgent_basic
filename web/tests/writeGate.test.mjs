/* 写权限纯逻辑的测试：node --test 直接跑（Node 24 自带 .ts 类型擦除，不引测试框架）。
   跑法：cd web && npm test
   这里钉的是界面上"能不能点"的判定，真正拦人的是后端 —— 但按钮说谎同样致命：
   界面上可点、点了 403，比按钮灰着更让人困惑。 */
import test from 'node:test'
import assert from 'node:assert/strict'
import {
  AUDIT_STATUS_LABEL, TIER_LABEL, backupPattern, effectiveTier, executeReady,
  gateNotice, isStalePlan, noRollbackNote, sourceCeiling, tierChoices,
} from '../src/lib/writeGate.ts'

const ds = (over = {}) => ({ id: 1, name: '本地订单库', type: 'mysql', allowed_ops: 'read', ...over })

/** 后端 /prepare_write 的出参形状（passed=true 的正常计划） */
function plan(over = {}) {
  return {
    passed: true, reason: null, statement_kind: 'update', statement_label: '更新 UPDATE',
    targets: [{ database: 'biz', table: 't_order', is_main: true }],
    destructive: false, confirm_phrase: '', backup_table: '_bak_t_order_x', backup_sql: 'CREATE …',
    estimate_sql: 'SELECT COUNT(*) …', needs_affected_rows: true, final_sql: 'UPDATE t_order SET …',
    estimated_rows: 3, requested_tier: 'write_dml', effective_tier: 'write_dml',
    max_allowed_rows: 5000,
    ...over,
  }
}

test('文件源与缺省值一律停在只读', () => {
  assert.equal(sourceCeiling(null), 'read')
  assert.equal(sourceCeiling(undefined), 'read')
  assert.equal(sourceCeiling(ds({ allowed_ops: undefined })), 'read')
  // 文件源即使被标成可写也压回 read：SQLite 以 mode=ro 打开，物理上写不了
  assert.equal(sourceCeiling(ds({ type: 'file', allowed_ops: 'write_ddl' })), 'read')
  assert.equal(sourceCeiling(ds({ allowed_ops: 'write_ddl' })), 'write_ddl')
})

test('请求档位高于源上限时被压低，不会自己抬', () => {
  assert.equal(effectiveTier('write_ddl', 'read'), 'read')
  assert.equal(effectiveTier('write_ddl', 'write_dml'), 'write_dml')
  assert.equal(effectiveTier('write_dml', 'write_ddl'), 'write_dml')
  assert.equal(effectiveTier('read', 'write_ddl'), 'read')
})

test('超过源上限的档位禁选，并且写明原因', () => {
  const locked = tierChoices('read')
  assert.deepEqual(locked.map((c) => c.disabled), [false, true, true])
  assert.match(locked[1].why, /数据源上限/)
  assert.match(locked[2].why, /数据源上限/)
  assert.equal(locked[0].why, '')
  const open = tierChoices('write_ddl')
  assert.deepEqual(open.map((c) => c.disabled), [false, false, false])
  assert.equal(TIER_LABEL.write_ddl, '结构变更')
})

test('总闸关着时生效档位恒为只读，高档位禁选且原因先说总闸', () => {
  // 按钮写着"结构变更"、点下去 403，比按钮一直灰着更让人困惑
  assert.equal(effectiveTier('write_ddl', 'write_ddl', false), 'read')
  assert.equal(effectiveTier('write_dml', 'write_ddl', false), 'read')
  const locked = tierChoices('write_ddl', false)
  assert.deepEqual(locked.map((c) => c.disabled), [false, true, true])
  assert.match(locked[1].why, /写总闸未开/)
  // 总闸是外层：源上限也不够时，原因仍然先说总闸
  assert.match(tierChoices('read', false)[2].why, /写总闸未开/)
})

test('健康检查没回来（writesEnabled=null）时不擅自压成只读，也不冒充已开', () => {
  assert.equal(effectiveTier('write_ddl', 'write_ddl', null), 'write_ddl')
  assert.equal(effectiveTier('write_ddl', 'write_dml', null), 'write_dml')
  assert.deepEqual(tierChoices('write_ddl', null).map((c) => c.disabled), [false, false, false])
  // 缺省参数与显式 null 同义：老调用点不会因为签名变了而改行为
  assert.equal(effectiveTier('write_ddl', 'write_dml'), 'write_dml')
})

test('一句话说明能不能写：四层约束哪一层没对上就说哪一层', () => {
  assert.equal(gateNotice({ ds: null, requested: 'write_dml', ceiling: 'read',
                            writesEnabled: true }).tone, 'warn')
  // 只读档不该吓人
  const ro = gateNotice({ ds: ds(), requested: 'read', ceiling: 'read', writesEnabled: false })
  assert.equal(ro.tone, 'ok')
  assert.match(ro.text, /只读/)
  // 总闸没开：档位选得再高也写不下去，必须说清是哪个环境变量
  const gate = gateNotice({ ds: ds({ allowed_ops: 'write_ddl' }), requested: 'write_ddl',
                            ceiling: 'write_ddl', writesEnabled: false })
  assert.equal(gate.tone, 'bad')
  assert.match(gate.text, /AGENT_ALLOW_WRITES/)
  // 健康检查还没回来（writesEnabled=null）：不许冒充"总闸已开"
  const unknown = gateNotice({ ds: ds({ allowed_ops: 'write_ddl' }), requested: 'write_ddl',
                               ceiling: 'write_ddl', writesEnabled: null, maxRows: 5000 })
  assert.equal(unknown.tone, 'warn')
  assert.match(unknown.text, /未知/)
  const known = gateNotice({ ds: ds({ allowed_ops: 'write_ddl' }), requested: 'write_ddl',
                             ceiling: 'write_ddl', writesEnabled: true, maxRows: 5000 })
  assert.equal(known.tone, 'ok')
  assert.match(known.text, /5000/)
  assert.equal(gateNotice({ ds: ds({ type: 'file', allowed_ops: 'write_ddl' }),
                            requested: 'write_dml', ceiling: 'read', writesEnabled: true }).tone, 'bad')
  assert.equal(gateNotice({ ds: ds({ allowed_ops: 'write_dml' }), requested: 'write_ddl',
                            ceiling: 'write_dml', writesEnabled: true, maxRows: 100 }).tone, 'warn')
})

test('没有预览就没有执行按钮', () => {
  const r = executeReady(null, true, '')
  assert.equal(r.ok, false)
  assert.deepEqual(r.blockers, ['先点「预览影响面」'])
})

test('勾选确认位是执行的前置条件', () => {
  assert.equal(executeReady(plan(), false, '').ok, false)
  assert.match(executeReady(plan(), false, '').blockers.join(' '), /勾选/)
  assert.equal(executeReady(plan(), true, '').ok, true)
})

test('破坏性操作要原样输入表名，空格之外不容错', () => {
  const p = plan({ statement_kind: 'drop', statement_label: '删对象 DROP', destructive: true,
                   confirm_phrase: 't_order', estimated_rows: null })
  assert.equal(executeReady(p, true, '').ok, false)
  assert.match(executeReady(p, true, 't_orde').blockers.join(' '), /原样输入表名 t_order/)
  assert.equal(executeReady(p, true, 't_order').ok, true)
  assert.equal(executeReady(p, true, '  t_order ').ok, true)     // 首尾空格不算输错
  assert.equal(executeReady(p, true, 'T_ORDER').ok, false)       // 表名大小写不容忍
})

test('预估超过单次上限就拦下，等于上限不算超', () => {
  assert.equal(executeReady(plan({ estimated_rows: 5000, max_allowed_rows: 5000 }), true, '').ok, true)
  const over = executeReady(plan({ estimated_rows: 5001, max_allowed_rows: 5000 }), true, '')
  assert.equal(over.ok, false)
  assert.match(over.blockers.join(' '), /5001 行/)
  // 后端拿不到上限（0）时不拿它判：真正的上限判定在执行器里
  assert.equal(executeReady(plan({ estimated_rows: 99999, max_allowed_rows: 0 }), true, '').ok, true)
  // 预估不出来（null）也不在这层拦：后端预估失败是 fail-closed
  assert.equal(executeReady(plan({ estimated_rows: null }), true, '').ok, true)
})

test('预览之后改了 SQL / 库 / 档位，那份预览立刻作废', () => {
  const cur = { sql: 'UPDATE t_order SET city=\'x\' WHERE id>0', db: 'biz', tier: 'write_dml' }
  assert.equal(isStalePlan(null, cur), true)
  assert.equal(isStalePlan(cur, cur), false)
  // 只是多了分号/换了空白，不算改
  assert.equal(isStalePlan({ ...cur, sql: `${cur.sql};` }, cur), false)
  assert.equal(isStalePlan({ ...cur, sql: cur.sql.replace(/SET/g, 'SET ') }, cur), false)
  assert.equal(isStalePlan({ ...cur, sql: 'UPDATE t_order SET city=\'y\' WHERE id>0' }, cur), true)
  assert.equal(isStalePlan({ ...cur, db: 'other' }, cur), true)
  assert.equal(isStalePlan({ ...cur, tier: 'write_ddl' }, cur), true)
})

test('审计三态都有中文口径（rejected 也单独列）', () => {
  assert.deepEqual(Object.keys(AUDIT_STATUS_LABEL).sort(),
                   ['executed', 'failed', 'rejected'])
  assert.equal(AUDIT_STATUS_LABEL.rejected, '被拒绝')
})

test('预览里的备份表名显示成"形如"，不当成执行时真会用的名字', () => {
  assert.equal(backupPattern('_bak_ui_order_20261028_012603_d2c667'), '_bak_ui_order_…')
  assert.equal(backupPattern(null), '')
  // 认不出尾巴就原样给，别把正常表名砍掉半截
  assert.equal(backupPattern('_bak_odd_name'), '_bak_odd_name')
})

test('没有回滚指引时，CREATE 与 INSERT 各自说清怎么撤销', () => {
  // INSERT 的行根本不在备份表里，套用"新建对象直接删掉它"等于没告诉人怎么删
  assert.match(noRollbackNote('insert'), /不在备份表里/)
  assert.match(noRollbackNote('insert'), /按主键删掉/)
  assert.match(noRollbackNote('create'), /DROP/)
  assert.ok(noRollbackNote('weird_kind').length > 0)
  assert.notEqual(noRollbackNote('insert'), noRollbackNote('create'))
})
