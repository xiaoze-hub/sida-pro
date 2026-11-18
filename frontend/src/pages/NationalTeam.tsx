/**
 * 国家队动向工作区（2026-11-18）。
 *
 * 目的：把「汇金 / 证金 / 外汇局三平台这个季度拿了什么、加了谁减了谁」做成一个能独立打开的
 * 页面，而不是散在个股 F10 的十大股东里一列姓名。
 *
 * 数据契约（来源是代码，不是猜 —— 见 `src/web/api/national_team.py` 与
 * `src/core/national_team.py` 模块 docstring）：
 *  · `GET /api/national-team/overview`  → 主体汇总 + 报告期列表 + 未在榜主体 + 采集时点
 *  · `GET /api/national-team/holdings`  → 明细（holder/q/sort/limit）
 *  · `GET /api/national-team/changes`   → 环比（new / increase / decrease / exit）
 *  · `POST /api/national-team/refresh`  → owner 手动采集，返回 receipt
 *
 * 三条不可退让的口径（与后端一致，页面必须显式展示，不得省略）：
 *  ① **季报口径**：`report_date` 是报告期末，不是实时持仓；页面同时显示采集时点；
 *  ② **只含进入前十大股东/十大流通股东的持仓**，第 11 名以后上游不提供；
 *  ③ **不含宽基 ETF**：汇金真正的重仓（沪深300 等）只在基金半年报/年报「前十大持有人」披露，
 *     没有公开汇总 API ⇒ 本页是**个股直投**口径，不能当成国家队总持仓；
 *  另：A+H 混行（如建行）未拆分，市值取源端原值。
 *
 * 未在榜主体（外汇局三平台 2020 年后已退出）单独列出「本期未出现在十大股东」，
 * 不隐藏、不算成采集失败。
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { AlertCircle, Loader2, RefreshCw } from 'lucide-react'
import {
  nationalTeamApi,
  type NationalTeamChangeItem,
  type NationalTeamChangesResponse,
  type NationalTeamHolding,
  type NationalTeamOverview,
  type NationalTeamReportKind,
} from '@sida/api'
import PageHeader from '@/components/PageHeader'
import { Button } from '@sida/base-ui/components/ui/button'
import { StatusChip } from '@sida/base-ui/components/ui/status-chip'
import { safeFixed, safeInt, safeMoneyUnsigned, safePercent } from '@/lib/format'
import { cn } from '@/lib/utils'

type LoadStatus = 'idle' | 'loading' | 'ready' | 'error'

const KIND_OPTIONS: Array<{ value: NationalTeamReportKind; label: string; hint: string }> = [
  { value: 'float', label: '十大流通股东', hint: '覆盖更全（进不了十大股东但进了十大流通股东的也会出现）' },
  { value: 'total', label: '十大股东', hint: '含持股比例，银行类大股东口径' },
]

const CHANGE_LABEL: Record<NationalTeamChangeItem['kind'], string> = {
  new: '新进',
  increase: '加仓',
  decrease: '减仓',
  exit: '退出',
  unchanged: '未变',
}

const CHANGE_TONE: Record<NationalTeamChangeItem['kind'], string> = {
  new: 'text-stock-up',
  increase: 'text-stock-up',
  decrease: 'text-stock-down',
  exit: 'text-stock-down',
  unchanged: 'text-muted-foreground',
}

function errText(e: unknown): string {
  if (e instanceof Error && e.message) return e.message
  return '请求失败（未收到有效响应）'
}

function fmtSnapshot(iso: string | null | undefined): string {
  if (!iso) return '未知'
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return iso
  return new Intl.DateTimeFormat('zh-CN', {
    timeZone: 'Asia/Shanghai',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
  }).format(d)
}

/** 持股数（股）→ 亿股 / 万股；与后端单位一致（股），前端只做展示换算。 */
function fmtShares(v: number | null | undefined): string {
  if (v === null || v === undefined || !Number.isFinite(v)) return '--'
  const abs = Math.abs(v)
  if (abs >= 1e8) return `${safeFixed(v / 1e8, 2)} 亿股`
  if (abs >= 1e4) return `${safeFixed(v / 1e4, 2)} 万股`
  return `${safeInt(v)} 股`
}

function ChangeRow({ item }: { item: NationalTeamChangeItem }) {
  return (
    <div className="flex items-center justify-between gap-3 px-3 py-2 hover:bg-s2">
      <div className="flex min-w-0 items-center gap-2">
        <span className={cn('w-10 shrink-0 text-[12px] font-medium', CHANGE_TONE[item.kind])}>
          {CHANGE_LABEL[item.kind]}
        </span>
        <span className="min-w-0 truncate text-[14px]">
          <span className="tabular-nums text-muted-foreground">{item.stock_code}</span>
          <span className="ml-2">{item.stock_name || '—'}</span>
        </span>
        <span className="hidden shrink-0 text-[12px] text-muted-foreground sm:inline">{item.holder_group}</span>
      </div>
      <div className="flex shrink-0 items-center gap-3 text-[12px] tabular-nums">
        <span className="text-muted-foreground">{fmtShares(item.prev_num)}</span>
        <span className="text-muted-foreground">→</span>
        <span className="text-[14px]">{fmtShares(item.cur_num)}</span>
        <span className={cn('w-24 text-right', CHANGE_TONE[item.kind])}>
          {item.delta_num > 0 ? '+' : ''}
          {fmtShares(item.delta_num)}
        </span>
      </div>
    </div>
  )
}

function HoldingRow({ item }: { item: NationalTeamHolding }) {
  return (
    <div className="grid grid-cols-[minmax(0,1fr)_repeat(3,minmax(72px,auto))_minmax(56px,auto)] items-center gap-3 px-3 py-2 hover:bg-s2">
      <div className="min-w-0">
        <div className="truncate text-[14px]">
          <span className="tabular-nums text-muted-foreground">{item.stock_code}</span>
          <span className="ml-2">{item.stock_name || '—'}</span>
        </div>
        <div className="mt-0.5 flex flex-wrap items-center gap-1.5 text-[12px] text-muted-foreground">
          <span>{item.holder_group}</span>
          {item.shares_type && <span>· {item.shares_type}</span>}
          {item.change_type && item.change_type !== '不变' && (
            <span className="text-foreground">· {item.change_type}</span>
          )}
        </div>
      </div>
      <div className="text-right text-[14px] tabular-nums">{fmtShares(item.hold_num)}</div>
      <div className="text-right text-[14px] tabular-nums">{safePercent(item.hold_ratio, 2, false)}</div>
      <div className="text-right text-[14px] tabular-nums">{safeMoneyUnsigned(item.market_cap)}</div>
      <div className="text-right text-[12px] tabular-nums text-muted-foreground">
        {item.holder_rank ? `第 ${item.holder_rank} 大` : '—'}
      </div>
    </div>
  )
}

export default function NationalTeam() {
  const [kind, setKind] = useState<NationalTeamReportKind>('float')
  const [overview, setOverview] = useState<NationalTeamOverview | null>(null)
  const [holdings, setHoldings] = useState<NationalTeamHolding[]>([])
  const [changes, setChanges] = useState<NationalTeamChangesResponse | null>(null)
  const [holder, setHolder] = useState<string>('all')
  const [status, setStatus] = useState<LoadStatus>('idle')
  const [error, setError] = useState<string | null>(null)
  const [refreshing, setRefreshing] = useState(false)
  const [refreshNote, setRefreshNote] = useState<string | null>(null)
  // 竞态守卫：切口径/切主体会连发请求，慢响应的旧请求必须丢弃（否则表与汇总对不上）
  const seq = useRef(0)

  const load = useCallback(
    async (nextKind: NationalTeamReportKind, nextHolder: string) => {
      const my = ++seq.current
      setStatus('loading')
      setError(null)
      try {
        const [ov, list, ch] = await Promise.all([
          nationalTeamApi.overview({ kind: nextKind }),
          nationalTeamApi.holdings({ kind: nextKind, holder: nextHolder, limit: 300 }),
          nationalTeamApi.changes({ kind: nextKind, limit: 60 }),
        ])
        if (my !== seq.current) return
        setOverview(ov)
        setHoldings(list.items)
        setChanges(ch)
        setStatus('ready')
      } catch (e) {
        if (my !== seq.current) return
        setOverview(null)
        setHoldings([])
        setChanges(null)
        setError(errText(e))
        setStatus('error')
      }
    },
    [],
  )

  useEffect(() => {
    void load(kind, holder)
  }, [kind, holder, load])

  const onRefresh = useCallback(async () => {
    setRefreshing(true)
    setRefreshNote(null)
    try {
      const receipt = await nationalTeamApi.refresh()
      const errs = receipt.errors?.length ?? 0
      setRefreshNote(
        receipt.ok
          ? `采集完成：${receipt.periods?.join(' / ') || '—'}，写入 ${receipt.written} 行${
              errs ? `，${errs} 个目标失败` : ''
            }`
          : `采集部分失败：写入 ${receipt.written} 行，${errs} 个目标失败（见后端日志）`,
      )
      await load(kind, holder)
    } catch (e) {
      setRefreshNote(`采集失败：${errText(e)}`)
    } finally {
      setRefreshing(false)
    }
  }, [kind, holder, load])

  const totals = overview?.totals
  const holderOptions = useMemo(() => {
    const opts = [{ key: 'all', label: '全部主体' }]
    for (const g of overview?.groups ?? []) opts.push({ key: g.holder_key, label: g.holder_group })
    return opts
  }, [overview])

  return (
    <div className="page-container space-y-4">
      <PageHeader
        title="国家队动向"
        description="汇金 / 证金 / 外汇局三平台的十大股东持仓与季度环比。季报口径，不是实时持仓。"
        actions={
          <Button variant="ghost" size="sm" onClick={() => void onRefresh()} disabled={refreshing}>
            {refreshing ? <Loader2 className="mr-1 h-4 w-4 animate-spin" /> : <RefreshCw className="mr-1 h-4 w-4" />}
            采集最新
          </Button>
        }
        metadata={
          <>
            <StatusChip tone={overview?.available ? 'info' : 'neutral'}>
              报告期 {overview?.report_date ?? '无数据'}
            </StatusChip>
            {overview?.prev_report_date && <StatusChip>环比基准 {overview.prev_report_date}</StatusChip>}
            <StatusChip tone="neutral">采集 {fmtSnapshot(overview?.snapshot_at)}</StatusChip>
            <StatusChip tone="neutral">源 东方财富十大股东</StatusChip>
          </>
        }
      />

      {/* 口径声明常驻：这三句话是该页能被信任的前提，不折叠、不缩字 */}
      <div className="space-y-1 rounded-lg bg-s1 p-3 text-[12px] leading-5 text-muted-foreground">
        <div>{overview?.caliber_note ?? '口径：东方财富十大股东/十大流通股东（季报披露），只含进入前十大的持仓；不含宽基 ETF 持仓；A+H 未拆分。'}</div>
        <div>
          {overview?.holder_scope_note ??
            '覆盖主体：中央汇金投资、中央汇金资管、证金公司、外汇局三平台（梧桐树/凤山/坤藤）。'}
          合计是个股直投口径，不等于国家队总持仓。
        </div>
      </div>

      {refreshNote && <div className="rounded-lg bg-s2 px-3 py-2 text-[12px]">{refreshNote}</div>}

      {/* 口径切换 */}
      <div className="flex flex-wrap items-center gap-2">
        <div className="inline-flex rounded-lg bg-s1 p-1">
          {KIND_OPTIONS.map((o) => (
            <button
              key={o.value}
              type="button"
              onClick={() => setKind(o.value)}
              title={o.hint}
              className={cn(
                'rounded-md px-3 py-1.5 text-[14px]',
                kind === o.value ? 'bg-s3 font-medium text-foreground' : 'text-muted-foreground hover:bg-s2',
              )}
            >
              {o.label}
            </button>
          ))}
        </div>
        {status === 'loading' && (
          <span className="inline-flex items-center gap-1 text-[12px] text-muted-foreground">
            <Loader2 className="h-3.5 w-3.5 animate-spin" /> 加载中
          </span>
        )}
      </div>

      {status === 'error' && (
        <div className="flex items-start gap-2 rounded-lg bg-s1 p-3 text-[14px]">
          <AlertCircle className="mt-0.5 h-4 w-4 text-destructive" />
          <div className="min-w-0 flex-1">
            <div>国家队数据加载失败：{error}</div>
            <Button variant="outline" size="sm" className="mt-2" onClick={() => void load(kind, holder)}>
              重试
            </Button>
          </div>
        </div>
      )}

      {status !== 'error' && overview && !overview.available && (
        <div className="rounded-lg bg-s1 p-3 text-[14px]">
          <div>{overview.reason || '暂无国家队持仓数据'}</div>
          <div className="mt-1 text-[12px] text-muted-foreground">
            首次使用需点右上角「采集最新」（owner 权限）或等每日 19:40 的自动同步。
          </div>
        </div>
      )}

      {overview?.available && (
        <>
          {/* 主体汇总：底色 + 留白分层，不描边、不套卡片 */}
          <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-4">
            <div className="rounded-lg bg-s2 p-3">
              <div className="text-[12px] text-muted-foreground">合计（个股直投）</div>
              <div className="mt-1 text-[20px] font-semibold tabular-nums">
                {safeMoneyUnsigned(totals?.market_cap)}
              </div>
              <div className="mt-1 text-[12px] text-muted-foreground">
                {totals?.stocks ?? 0} 只 · 持股 {fmtShares(totals?.hold_num)}
                {!!totals?.market_cap_missing && ` · ${totals.market_cap_missing} 只缺市值`}
              </div>
            </div>
            {overview.groups.map((g) => (
              <div key={g.holder_key} className="rounded-lg bg-s1 p-3">
                <div className="flex items-center justify-between gap-2">
                  <span className="text-[12px] text-muted-foreground">{g.holder_group}</span>
                  {g.changed_stocks > 0 && (
                    <StatusChip tone="warning">{g.changed_stocks} 只变动</StatusChip>
                  )}
                </div>
                <div className="mt-1 text-[16px] font-semibold tabular-nums">{safeMoneyUnsigned(g.market_cap)}</div>
                <div className="mt-1 text-[12px] tabular-nums text-muted-foreground">
                  {g.stocks} 只 · {fmtShares(g.hold_num)}
                </div>
              </div>
            ))}
          </div>

          {!!overview.absent_holders?.length && (
            <div className="rounded-lg bg-s1 px-3 py-2 text-[12px] text-muted-foreground">
              本期未在榜：
              {overview.absent_holders.map((a) => `${a.holder_group}`).join('、')}
              —— 该主体本期未出现在十大股东/十大流通股东（已退出或未进前十），不是采集失败。
            </div>
          )}

          {/* 环比变动 */}
          <section className="rounded-lg bg-s1">
            <div className="flex flex-wrap items-center justify-between gap-2 px-3 pt-3">
              <h2 className="text-[16px] font-medium">环比变动</h2>
              {changes?.available && (
                <div className="flex flex-wrap items-center gap-1.5">
                  <StatusChip tone="success">新进 {changes.counts.new}</StatusChip>
                  <StatusChip tone="success">加仓 {changes.counts.increase}</StatusChip>
                  <StatusChip tone="danger">减仓 {changes.counts.decrease}</StatusChip>
                  <StatusChip tone="danger">退出 {changes.counts.exit}</StatusChip>
                  <StatusChip tone="neutral">未变 {changes.counts.unchanged}</StatusChip>
                </div>
              )}
            </div>
            <div className="mt-2 divide-y divide-border">
              {!changes?.available && (
                <div className="px-3 py-3 text-[14px] text-muted-foreground">
                  {changes?.reason || '暂无可比报告期'}
                </div>
              )}
              {changes?.items.slice(0, 20).map((it) => (
                <ChangeRow key={`${it.holder_key}-${it.stock_code}-${it.kind}`} item={it} />
              ))}
              {changes?.available && changes.items.length === 0 && (
                <div className="px-3 py-3 text-[14px] text-muted-foreground">
                  本期 {changes.counts.unchanged} 只全部持股数未变
                </div>
              )}
            </div>
            {changes?.truncated && (
              <div className="px-3 py-2 text-[12px] text-muted-foreground">
                变动共 {changes.count} 条，此处只显示前 60 条（按变动幅度排序）。
              </div>
            )}
          </section>

          {/* 明细 */}
          <section className="rounded-lg bg-s1">
            <div className="flex flex-wrap items-center justify-between gap-2 px-3 pt-3">
              <h2 className="text-[16px] font-medium">
                持仓明细
                <span className="ml-2 text-[12px] font-normal text-muted-foreground">
                  {holdings.length} / {overview.report_kind_label}
                </span>
              </h2>
              <div className="inline-flex flex-wrap gap-1 rounded-lg bg-s2 p-1">
                {holderOptions.map((o) => (
                  <button
                    key={o.key}
                    type="button"
                    onClick={() => setHolder(o.key)}
                    className={cn(
                      'rounded-md px-2.5 py-1 text-[12px]',
                      holder === o.key ? 'bg-s3 font-medium text-foreground' : 'text-muted-foreground hover:bg-s3',
                    )}
                  >
                    {o.label}
                  </button>
                ))}
              </div>
            </div>
            <div className="mt-2 divide-y divide-border">
              <div className="grid grid-cols-[minmax(0,1fr)_repeat(3,minmax(72px,auto))_minmax(56px,auto)] gap-3 px-3 py-1.5 text-[12px] text-muted-foreground">
                <span>标的 / 主体</span>
                <span className="text-right">持股数</span>
                <span className="text-right">占总股本</span>
                <span className="text-right">市值</span>
                <span className="text-right">排名</span>
              </div>
              {holdings.map((h) => (
                <HoldingRow key={`${h.holder_key}-${h.stock_code}`} item={h} />
              ))}
              {!holdings.length && (
                <div className="px-3 py-3 text-[14px] text-muted-foreground">该主体本期无可显示持仓</div>
              )}
            </div>
            {kind === 'float' && (
              <div className="px-3 pb-3 text-[12px] text-muted-foreground">
                十大流通股东表源端不提供持股比例（`占总股本` 显示为 --）；需要比例切到「十大股东」口径。
              </div>
            )}
          </section>
        </>
      )}
    </div>
  )
}
