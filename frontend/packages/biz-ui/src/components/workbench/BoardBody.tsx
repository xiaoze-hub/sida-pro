import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { fetchAPI } from '@panwatch/api'
import { readStockColors } from '@panwatch/biz-ui/lib/stock-colors'
import { safeFixed } from '@/lib/format'

/**
 * 板块正文(Task 7, 三合一 spec §1.3): 从 `pages/BoardDetail.tsx` **原样搬移**的取数与渲染。
 *
 * 归属 `packages/biz-ui/src/components/workbench/`(计划 §文件结构): 正文只依赖
 * `@panwatch/api` / `@panwatch/biz-ui/lib/stock-colors` / `@/lib/format` —— 后者的 biz-ui → `@/lib/*`
 * 依赖是本仓既有形态(`kline-summary-dialog.tsx:6`、`workbench/HeaderBand.tsx:8`), 包边界不新开边。
 * (兄弟 `IndexBody` 需 `@/components/ErrorBanner`(app 层), 故落在 `src/pages/workbench/`。)
 *
 * 与旧页的差异(仅两处, 均非业务逻辑):
 *  1. **去掉页面骨架**(返回按钮/标题/刷新): spec §1.3 末条「抽取前的页面骨架并入共享 HeaderBand,
 *     避免重复」—— 工作台带1 提供类型切换 + 刷新(刷新经 `onRefresh` 广播到本正文); **本正文
 *     自己拥有板块名称与数值**(`/boards/{code}`)—— 带1 对 `type !== 'stock'` **不取**
 *     `/quotes/{s}`(同代码 = 另一标的)也不渲染名称/现价, 见 `HeaderBand` 头注「同码不同标的闸门」;
 *  2. 数字格式化改走 `@/lib/format` 的 safe* 系列 (项目红线 #6 / R6 禁裸 toFixed):
 *     每处外部守卫不变, 输出字符串逐字相同。
 *  3. **新增可选 `refreshToken`(v0.6.0 遗留⑦)**: 页面级刷新改为 `refreshToken={refreshKey}`
 *     而**不是** `key={refreshKey}` —— 换 key 会卸载并重建整棵正文子树, 于是每次点刷新都重放
 *     `sida-page-enter` 入场动画(视觉"闪一下")并丢掉正文自己的内部 UI 状态。现在 token 变化只
 *     **重跑取数 effect**(见下), 组件实例与 DOM 节点都保持不动。不传该 prop 时行为与旧版一致
 *     (只在挂载/换标的时取数)。
 *
 * 板块详情(spec 口径不变, 2026-08-20 v0.3.0 / 方案B 2026-09-10):
 *   GET /boards/{code}              板块详情(今日 change_pct / fund_net / volume)
 *   GET /boards/{code}/constituents 成分股(通达信实时 / thsdk 实时)
 *   GET /boards/rotation?days=5     板块轮动排序(取 Top5 横条 + 本板块 5 日涨幅)
 *   - 通达信板块(88xxxx.SH): 今日涨跌幅/主力资金/成交额 + 成分股涨幅 均为**通达信客户端实时**
 *     (get_pricevol + AMO/SUPAMO 公式批量, 本地无配额); 资金为"通达信主力资金"口径,
 *     与 thsdk 主力净流入定义不同, 不混用。
 *   - thsdk 板块(URFI*): 沿用日线/扩展档口径。
 *   - 5 日涨幅 = 轮动接口(thsdk 日线复利), 通达信板块暂不命中(显示 --)。
 * 成分股: 通达信路径为规范字段(symbol/name/price/change_pct/amount); thsdk 路径为原始列,
 * 前端按语义模糊取列, 缺字段显示 --。
 */

interface BoardToday {
  date?: string | null
  change_pct?: number | null
  fund_net?: number | null
  volume?: number | null
}

interface BoardDetailResp {
  block_code: string
  name: string
  board_type?: string
  source?: string
  today?: BoardToday | null
  has_daily?: boolean
  live?: boolean
  error?: string
}

interface BoardConstituent {
  count: number
  source?: string
  items: Record<string, unknown>[]
}

interface RotationItem {
  block_code: string
  name: string
  rotation_score: number
  change_5d?: number | null
  fund_net?: number | null
  consecutive_days?: number
}

interface RotationResp {
  days: number
  items: RotationItem[]
}

/** 从 thsdk 原始列 dict 按中文/英文语义取字段 */
function pickNum(row: Record<string, unknown>, ...keys: string[]): number | null {
  for (const k of keys) {
    const v = row[k]
    if (v == null) continue
    const n = typeof v === 'number' ? v : Number(String(v).replace(/[^\d.-]/g, ''))
    if (Number.isFinite(n)) return n
  }
  return null
}

function pickStr(row: Record<string, unknown>, ...keys: string[]): string {
  for (const k of keys) {
    const v = row[k]
    if (v != null && String(v).trim() !== '') return String(v).trim()
  }
  return ''
}

/** 资金净额(元) -> 亿/万 紧凑带符号 */
function fmtWan(v: number | null | undefined): string {
  if (v == null || !Number.isFinite(v)) return '--'
  const abs = Math.abs(v)
  const sign = v > 0 ? '+' : v < 0 ? '-' : ''
  if (abs >= 1e8) return `${sign}${safeFixed(abs / 1e8, 2)}亿`
  return `${sign}${safeFixed(abs / 1e4, 0)}万`
}

function fmtPct(v: number | null | undefined, plus = true): string {
  if (v == null || !Number.isFinite(v)) return '--'
  return `${plus && v > 0 ? '+' : ''}${safeFixed(v, 2)}%`
}

function pctColor(v: number | null | undefined): string {
  if (v == null || !Number.isFinite(v)) return 'text-muted-foreground'
  // 涨跌色统一走设计令牌 --stock-up/--stock-down (红涨绿跌, A股口径)
  return v > 0 ? 'text-stock-up' : v < 0 ? 'text-stock-down' : 'text-muted-foreground'
}

export default function BoardBody({ code, refreshToken }: { code: string; refreshToken?: number }) {
  const navigate = useNavigate()
  const [detail, setDetail] = useState<BoardDetailResp | null>(null)
  const [constituents, setConstituents] = useState<BoardConstituent | null>(null)
  const [rotation, setRotation] = useState<RotationResp | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  /** `load()` 竞态守卫的取号器(见 `load` 内注): 只认最新一次取数的结果。 */
  const seqRef = useRef(0)

  const load = useCallback(async () => {
    if (!code) return
    // 竞态守卫(遗留⑦ 复审 Finding 2): 改用 `refreshToken` 后本组件实例**常驻**(不再被 key 重挂载
    // 丢弃在飞请求), 连点刷新 ⇒ 多个 `load()` 同时在飞。慢的旧请求后到就会用**旧数据/旧错误**
    // 覆盖新结果 —— 本组件 `error` 优先于 `detail` 渲染, 一次过期失败足以把好内容换成错误横幅。
    // 取号 → await 后只认最新号(形态同 `L2Tab.tsx` 的 `useL2Sources.seqRef`)。
    const seq = ++seqRef.current
    setLoading(true)
    setError('')
    try {
      const [d, c] = await Promise.all([
        fetchAPI<BoardDetailResp>(`/boards/${encodeURIComponent(code)}`, { cacheMode: 'reload' }),
        fetchAPI<BoardConstituent>(`/boards/${encodeURIComponent(code)}/constituents`, { cacheMode: 'reload' }).catch(() => null),
      ])
      if (seq !== seqRef.current) return
      if (d?.error) setError(d.error)
      else setDetail(d)
      setConstituents(c)
    } catch (e) {
      if (seq !== seqRef.current) return
      setError(e instanceof Error ? e.message : '板块详情加载失败')
    }
    // 板块轮动(全局, 独立加载, 失败静默) —— 过期号不许写 state
    fetchAPI<RotationResp>('/boards/rotation?days=5', { cacheMode: 'reload' })
      .then((r) => { if (seq === seqRef.current) setRotation(r) })
      .catch(() => {})
    // 过期号不清 loading(交给最新那次), 否则新请求还在飞就提前显示"加载完成"
    if (seq === seqRef.current) setLoading(false)
  }, [code])

  // 遗留⑦: `refreshToken` 变化 = 页面级刷新(带1 的刷新按钮)⇒ **只重跑取数**, 不重挂载组件。
  // 旧做法是页面给正文子树挂 `key={refreshKey}`: 换 key 会卸载并重建整棵子树 ⇒ 重放
  // `sida-page-enter` 入场动画(视觉上"闪一下")并丢掉正文自己的内部 UI 状态。
  useEffect(() => { void load() }, [load, refreshToken])

  const rotItemsTop = useMemo(() => (rotation?.items ?? []).slice(0, 5), [rotation])
  // 本板块 5 日涨幅(从轮动结果按 block_code 反查; 非本板块命中则为空)
  const selfRotation = useMemo(
    () => (rotation?.items ?? []).find((r) => r.block_code === code) ?? null,
    [rotation, code]
  )
  const fundNet = detail?.today?.fund_net ?? selfRotation?.fund_net
  // 轮动分数区间 → 颜色(涨红/跌绿, 强弱由 ± score 比例决定)
  const maxScore = Math.max(1, ...rotItemsTop.map((r) => r.rotation_score))

  const today = detail?.today ?? null
  const srcLabel = detail?.source === 'tdx' ? '通达信实时' : 'thsdk 日线'

  // 顶栏关键指标
  const metrics = [
    { label: '今日涨跌幅', value: fmtPct(today?.change_pct), cls: pctColor(today?.change_pct), note: srcLabel },
    { label: '5日涨幅', value: fmtPct(selfRotation?.change_5d), cls: pctColor(selfRotation?.change_5d), note: '轮动复利' },
    { label: '资金净流入', value: fmtWan(fundNet), cls: pctColor(fundNet), note: detail?.source === 'tdx' ? '通达信主力资金' : 'thsdk 日线' },
    { label: '成交额', value: today?.volume != null && Number.isFinite(today.volume) ? `${safeFixed(today.volume / 1e8, 2)}亿` : '--', cls: 'text-foreground', note: detail?.source === 'tdx' ? '通达信实时' : '换手率未提供' },
  ]

  const rotBarColor = (r: RotationItem): string => {
    const c = r.change_5d
    if (c == null || !Number.isFinite(c)) return '#60a5fa'
    // 涨跌色统一取 --stock-up/--stock-down 令牌 (红涨绿跌, A股口径)
    const sc = readStockColors()
    return c > 0 ? sc.up : c < 0 ? sc.down : '#94a3b8'
  }

  return (
    <div className="page-container sida-page-enter pb-10">
      {error && (
        <div className="card p-3 mb-4 text-[12px] text-amber-700 dark:text-amber-500">
          {error}
          {/* UX 走查 2026-09-15: 板块码无效/不存在时给下一步, 不留死路 */}
          {/不存在|不可用|无数据/.test(error) ? (
            <a href="/heatmap" className="ml-2 underline text-primary">
              去板块热力图选板块
            </a>
          ) : null}
        </div>
      )}
      {loading && !detail ? (
        <div className="grid gap-3">
          <div className="h-[92px] rounded-md border border-border/50 animate-pulse bg-accent/20" />
          <div className="h-[220px] rounded-md border border-border/50 animate-pulse bg-accent/20" />
        </div>
      ) : (
        <>
          {/* 关键指标 */}
          <div className="grid grid-cols-2 md:grid-cols-4 gap-3 mb-4">
            {metrics.map((m) => (
              <div key={m.label} className="border-l border-border/40 pl-3">
                <div className="text-[11px] text-muted-foreground">{m.label}</div>
                <div className={`text-[20px] font-bold mt-1 font-mono tabular-nums ${m.cls}`}>{m.value}</div>
                <div className="text-[10px] text-muted-foreground mt-1">{m.note}</div>
              </div>
            ))}
          </div>

          <div className="grid grid-cols-1 lg:grid-cols-3 gap-3">
            {/* 成分股表 */}
            <div className="lg:col-span-2 border-b border-border/40 pb-3 md:pb-4">
              <div className="flex items-center justify-between mb-2">
                <h2 className="text-[13px] font-semibold text-foreground">成分股</h2>
                <span className="text-[11px] text-muted-foreground">
                  共 {constituents?.count ?? 0} 只 · 口径 {constituents?.source === 'tdx' ? '通达信实时' : 'thsdk 实时'}
                </span>
              </div>
              <div className="overflow-x-auto">
                <table className="w-full text-[12px]">
                  <thead>
                    <tr className="text-[11px] text-muted-foreground border-b border-border/60">
                      <th className="text-left py-1.5 pr-2 w-8">#</th>
                      <th className="text-left py-1.5 pr-2">代码</th>
                      <th className="text-left py-1.5 pr-2">名称</th>
                      <th className="text-right py-1.5 pr-2">现价</th>
                      <th className="text-right py-1.5 pr-2">涨幅</th>
                      <th className="text-right py-1.5">成交额</th>
                    </tr>
                  </thead>
                  <tbody>
                    {(constituents?.items ?? []).map((row, i) => {
                      const rowCode = pickStr(row, 'symbol', '代码', 'code', '证券代码')
                      const name = pickStr(row, 'name', '名称', '证券名称')
                      // 通达信路径为规范字段(price/change_pct/amount); thsdk 路径回退语义模糊取列
                      const priceKey = Object.keys(row).find(
                        (k) => k.includes('现价') || k.includes('最新价') || k.toLowerCase() === 'price',
                      )
                      const chgKey = Object.keys(row).find((k) => /涨幅|涨跌|pct|change/i.test(k))
                      const amtKey = Object.keys(row).find((k) => /金额|成交额/.test(k) || /amount/i.test(k))
                      const price = pickNum(row, 'price', ...(priceKey ? [priceKey] : []))
                      const chg = pickNum(row, 'change_pct', ...(chgKey ? [chgKey] : []))
                      const amt = pickNum(row, 'amount', ...(amtKey ? [amtKey] : []))
                      return (
                        <tr key={rowCode || i} className="border-b border-border/30 hover:bg-accent/40">
                          <td className="py-1 pr-2 text-[10px] text-muted-foreground">{i + 1}</td>
                          <td className="py-1 pr-2 font-mono text-muted-foreground">{rowCode || '--'}</td>
                          <td className="py-1 pr-2 font-medium text-foreground">{name || '--'}</td>
                          <td className="py-1 pr-2 text-right font-mono tabular-nums text-muted-foreground">
                            {price == null ? '--' : safeFixed(price, 2)}
                          </td>
                          <td className={`py-1 pr-2 text-right font-mono tabular-nums ${pctColor(chg)}`}>{fmtPct(chg)}</td>
                          <td className="py-1 text-right font-mono tabular-nums text-muted-foreground">{fmtWan(amt)}</td>
                        </tr>
                      )
                    })}
                    {!constituents && (
                      <tr>
                        <td colSpan={6} className="py-8 text-center text-[11px] text-muted-foreground">
                          成分股暂不可用(数据源未接入 / 拉取失败)
                        </td>
                      </tr>
                    )}
                  </tbody>
                </table>
              </div>
            </div>

            {/* 板块轮动 Top5 横条 */}
            <div className="border-b border-border/40 pb-3 md:pb-4">
              <div className="flex items-center justify-between mb-3">
                <h2 className="text-[13px] font-semibold text-foreground">板块轮动 Top 5</h2>
                <span className="text-[10px] text-muted-foreground">近{rotation?.days ?? 5}日 · click进入详情</span>
              </div>
              {rotItemsTop.length === 0 ? (
                <div className="text-[11px] text-muted-foreground py-8 text-center">暂无轮动数据</div>
              ) : (
                <div className="space-y-2.5">
                  {rotItemsTop.map((r) => (
                    <button
                      key={r.block_code}
                      type="button"
                      className="w-full text-left group"
                      onClick={() => navigate(`/boards/${r.block_code}`)}
                      title={`${r.name} · 强度 ${safeFixed(r.rotation_score, 1)}`}
                    >
                      <div className="flex items-center justify-between text-[11px] mb-0.5">
                        <span className="truncate font-medium text-foreground group-hover:text-primary transition-colors">{r.name}</span>
                        <span className="font-mono ml-2 shrink-0">
                          <span className={pctColor(r.change_5d)}>{fmtPct(r.change_5d)}</span>
                          <span className="text-muted-foreground ml-1.5">{safeFixed(r.rotation_score, 0)}</span>
                        </span>
                      </div>
                      <div className="h-1.5 rounded-full bg-accent/40 overflow-hidden">
                        <div
                          className="h-full rounded-full transition-[width]"
                          style={{ width: `${(r.rotation_score / maxScore) * 100}%`, backgroundColor: rotBarColor(r) }}
                        />
                      </div>
                    </button>
                  ))}
                </div>
              )}
              <div className="mt-3 text-[10px] text-muted-foreground">强度分: 动量+资金+连续性 0-100 降序</div>
            </div>
          </div>
        </>
      )}
    </div>
  )
}
