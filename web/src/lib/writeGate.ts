/* 写权限的纯逻辑：档位比较 + 确认就绪判定，不碰 React，也不发请求。
   后端 write_security / QueryExecutor 才是真正拦人的地方；这里把同一套顺序先在
   界面上跑一遍，为的是让人在点「预览影响面」之前就知道"这个源这个档位能不能写、
   卡在哪一层"，而不是点了才收到一句 403。 */
import type { Datasource, PermTier, WritePlan } from '../api/types.ts'

export const TIER_ORDER: PermTier[] = ['read', 'write_dml', 'write_ddl']

export const TIER_LABEL: Record<PermTier, string> = {
  read: '只读',
  write_dml: '读写数据',
  write_ddl: '结构变更',
}

export const TIER_DESC: Record<PermTier, string> = {
  read: '只跑 SELECT，任何写语句都被拒',
  write_dml: '允许 INSERT / UPDATE / DELETE（改数据，不改表结构）',
  write_ddl: '再加 CREATE / ALTER / DROP / TRUNCATE（改结构，DROP 需手输表名）',
}

export function rankOf(tier: PermTier): number {
  return TIER_ORDER.indexOf(tier)
}

/** 数据源上限：文件源（SQLite 以 mode=ro 打开）物理上写不了，恒为 read。 */
export function sourceCeiling(ds: Pick<Datasource, 'type' | 'allowed_ops'> | null | undefined): PermTier {
  if (!ds || ds.type === 'file') return 'read'
  return ds.allowed_ops ?? 'read'
}

/** 有效档位 = 总闸关时恒为只读，否则 min(请求档位, 数据源上限)。
   总闸由环境变量决定，界面不能只报"人选的档位"：按钮写着结构变更、点下去 403，
   比按钮一直灰着更让人困惑。writesEnabled 为 null（健康检查还没回来）时不冒充已开，
   也不擅自压成只读——按源上限算，由 gateNotice 说明"状态未知"。 */
export function effectiveTier(
  requested: PermTier,
  ceiling: PermTier,
  writesEnabled: boolean | null = null,
): PermTier {
  if (writesEnabled === false) return 'read'
  return rankOf(requested) <= rankOf(ceiling) ? requested : ceiling
}

export interface TierChoice {
  tier: PermTier
  label: string
  desc: string
  disabled: boolean
  /** 禁选时给的原因（为什么这个档位在这台机器上现在点不动） */
  why: string
}

export function tierChoices(ceiling: PermTier, writesEnabled: boolean | null = null): TierChoice[] {
  return TIER_ORDER.map((tier) => {
    /* 总闸关着的时候，比只读高的档位一律禁选：让人点一下再吃 403，
       不如直接告诉他"这台机器现在写不下去"。总闸是外层，原因也先说它。 */
    const gateOff = writesEnabled === false && tier !== 'read'
    const overCeiling = rankOf(tier) > rankOf(ceiling)
    return {
      tier,
      label: TIER_LABEL[tier],
      desc: TIER_DESC[tier],
      disabled: gateOff || overCeiling,
      why: gateOff
        ? '写总闸未开（AGENT_ALLOW_WRITES=true 才开）：现在只能预览影响面'
        : overCeiling
          ? `数据源上限是「${TIER_LABEL[ceiling]}」，先在「数据源」里把它标成可写`
          : '',
    }
  })
}

export interface GateNotice {
  tone: 'ok' | 'warn' | 'bad'
  text: string
}

/** 一句话说明「这次到底能不能写」——四层约束里哪一层还没对上，就说哪一层。 */
export function gateNotice(input: {
  ds: Datasource | null
  requested: PermTier
  ceiling: PermTier
  writesEnabled: boolean | null
  maxRows?: number
}): GateNotice {
  const { ds, requested, ceiling, writesEnabled, maxRows } = input
  if (!ds) return { tone: 'warn', text: '未激活数据源：先连库或导入文件' }
  if (requested === 'read') {
    return { tone: 'ok', text: '只读模式：提问与任务清单都只会跑 SELECT' }
  }
  if (writesEnabled === false) {
    return {
      tone: 'bad',
      text: '写总闸未开（环境变量 AGENT_ALLOW_WRITES=true 才开）：现在只能预览，写不下去',
    }
  }
  if (writesEnabled === null) {
    return {
      tone: 'warn',
      text: '还没拿到后端健康检查，写总闸状态未知：可以先预览，能不能执行由后端定',
    }
  }
  if (ds.type === 'file') {
    return { tone: 'bad', text: '文件数据源只读（SQLite 以只读模式打开），改不了数据' }
  }
  if (rankOf(requested) > rankOf(ceiling)) {
    return {
      tone: 'warn',
      text: `数据源「${ds.name}」上限是「${TIER_LABEL[ceiling]}」：本次按上限执行，高于上限的语句会被拒`,
    }
  }
  return {
    tone: 'ok',
    text: `总闸已开 · 上限「${TIER_LABEL[ceiling]}」· 单次最多影响 ${maxRows ?? '—'} 行`,
  }
}

export interface ReadyState {
  ok: boolean
  blockers: string[]
}

/** 预览时的那条语句（含目标库、档位）。 */
export interface PreviewedRef {
  sql: string
  db: string
  tier: PermTier
}

/** 人改过 SQL / 换过库 / 换过档位之后，之前那份影响面预览就不作数了。
    后端执行前会重新安检重估，所以这不是安全边界，只是别让界面拿着旧数字说"预计影响 N 行"。 */
export function isStalePlan(previewed: PreviewedRef | null, cur: PreviewedRef): boolean {
  if (!previewed) return true
  const same = (a: string, b: string) => normSqlish(a) === normSqlish(b)
  return !(same(previewed.sql, cur.sql)
           && same(previewed.db, cur.db)
           && previewed.tier === cur.tier)
}

/** 只用于比较，不改原文：折叠空白、去掉末尾分号。 */
function normSqlish(s: string): string {
  return s.trim().replace(/\s+/g, ' ').replace(/;+$/, '').trim()
}

/** 确认就绪：预览通过 + 勾选确认 + 破坏性操作原样输入表名 + 预估没超上限。
   后端还会在执行前重估一次，这里只是把按钮的可用性对齐到同一条规则。 */
export function executeReady(
  plan: WritePlan | null,
  ack: boolean,
  typedPhrase: string,
): ReadyState {
  const blockers: string[] = []
  if (!plan) return { ok: false, blockers: ['先点「预览影响面」'] }
  if (!plan.passed) blockers.push('写入安检未通过')
  if (plan.estimated_rows !== null && plan.max_allowed_rows > 0
      && plan.estimated_rows > plan.max_allowed_rows) {
    blockers.push(
      `预计影响 ${plan.estimated_rows} 行，超过单次上限 ${plan.max_allowed_rows} 行`)
  }
  if (!ack) blockers.push('需要勾选「我已看清影响面」')
  if (plan.destructive && typedPhrase.trim() !== plan.confirm_phrase) {
    blockers.push(`需要原样输入表名 ${plan.confirm_phrase}`)
  }
  return { ok: blockers.length === 0, blockers }
}

/** 预览给出的备份表名带时间戳+随机位，执行时会另生成一个（同一秒写两次也不能撞名）。
   界面上得说成"形如 …"，否则人会拿预览里那个名字去找备份表，然后找不到。 */
export function backupPattern(name: string | null): string {
  if (!name) return ''
  return name.replace(/_\d{8}_\d{6}_[0-9a-f]{6}$/i, '_…')
}

/** 没有回滚指引时怎么解释：CREATE 与 INSERT 的"撤销"是两回事，一句通用话会把人带偏
   （INSERT 的行不在任何备份表里，说"新建对象直接删掉它"等于没告诉他怎么删）。 */
export function noRollbackNote(kind: string): string {
  if (kind === 'create') return '新建的对象没有可回滚的数据：要撤销就直接把它 DROP 掉。'
  if (kind === 'insert') return '插入的行不在备份表里（插入前它们并不存在）：要撤销就按主键删掉这几行。'
  return '这条语句没有对应的回滚指引。'
}

/** 审计三态的中文口径（rejected 也单独列：越权试探本身就是要看见的信号）。 */
export const AUDIT_STATUS_LABEL: Record<string, string> = {
  executed: '已执行',
  rejected: '被拒绝',
  failed: '执行失败',
}

export const AUDIT_STATUS_TONE: Record<string, 'ok' | 'bad' | 'warn'> = {
  executed: 'ok',
  rejected: 'warn',
  failed: 'bad',
}
