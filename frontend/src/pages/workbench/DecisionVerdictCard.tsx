import { useEffect, useRef, useState } from 'react'
import { insightApi } from '@panwatch/api'

/**
 * P0-3 决策合成接 UI(2026-10-10): `GET /api/decision/{symbol}` 三信号合成
 * (趋势 × 活跃度 × 资金 → **动手 / 看看 / 别碰** + 一行理由, 后端 `src/core/decision.py:synthesize`)。
 *
 * 断链背景: 该端点后端早已挂载(`src/web/app.py:673`)、前端 `insightApi.decision` 亦已定义
 * (`packages/api/src/insight.ts`), 但**全库零调用** —— 算得出来却没有任何页面显示。本组件是
 * 它**唯一**的消费方(接入工作台「研究」标签 `ResearchTab`, 页面外壳/带1/带2/其余标签**布局零改动**)。
 *
 * 三态诚实(never fabricate —— SIDA 铁律, 数据缺失不猜不编):
 *  - `loading` -> 显式加载文案(首拉在途**不**画成空、不预填 verdict);
 *  - `error`   -> 显式失败文案 + 原因(不猜方向、不落默认 verdict; 后端"永不 500"只保证算不出给
 *                 `看看`, **不保证网络/权限成功** —— 这里如实区分);
 *  - `ready`   -> `verdict` 恰为「动手/看看/别碰」之一才出徽标(三态徽标 + 一行理由); `reason`
 *                 缺失时显式「理由无数据」; `verdict` 缺失/表外(后端契约外) -> 显式「无数据」空态,
 *                 **不冒充**任何一个方向。
 *
 * 竞态与去重:
 *  - **换标的丢弃过期响应**: `seqRef` 取号, await 之后只认最新号(形态同 `L2Tab`/`IndexBody`),
 *    换 symbol 时旧响应一律不落地 —— 否则会把上一只票的结论画在新标的名下;
 *  - **重复渲染不重发**: 取数只挂在 `[symbol, market]` 上, 父组件(标签内)因无关状态重渲染
 *    不会重放请求; 另加 `fetchAPI` 自带 30s GET 缓存兜底(同键 30s 内网络零次)。
 *
 * 归属 `src/pages/workbench/`(与 `IndexBody` 同层): 属页面级装配, 不进 biz-ui —— 与兄弟组件的
 * 包边界一致(避免给 biz-ui 增加宿主隐性契约)。
 *
 * 个性化 + 跨市场(P1-2 / P2-7, 2026-10-10): 消费后端新增字段并**显式**上屏, 布局零改动:
 *  - `personalization_note` -> 个性化注记(哪部分因风险偏好调整, 可解释, `decision-personalization`);
 *  - `position.note` -> 持仓成本/浮盈参考行(`decision-position`), **不改 verdict 徽标**;
 *  - `basis === 'two-dimension'` -> 显式『资金维无数据(非CN)』(`decision-fund-missing`), 不冒充三信号。
 *  字段缺失(旧响应 / CN 无持仓)则这几行**不渲染** —— 三态徽标与理由行为与旧版逐字一致(向后兼容)。
 */
export interface DecisionVerdict {
  symbol?: string
  /** 后端三态之一: 「动手」/「看看」/「别碰」(表外值按异常处理, 不渲染为徽标) */
  verdict?: string
  /** 一行理由(含 verdict 前缀, 如「动手: 趋势G、活跃度26.68、主力流入2.30亿」) */
  reason?: string
  phase?: string
  row?: number
  parts?: { trend?: string; activity?: number | null; fund_net?: number | null }
  /** 跨市场降级标记(P2-7): 'two-dimension' = 非 CN 资金维无源, 仅趋势×活跃度双维 */
  basis?: string
  /** 双维时的显式缺资金标注(P2-7), 如「资金维无数据(非CN)」 */
  fund_note?: string
  /** 是否因个性化调整(P1-2) */
  personalized?: boolean
  /** 个性化调整说明(可解释), 如「个性化(保守型)：…」; 无调整为 null */
  personalization_note?: string | null
  risk_profile?: string | null
  /** 持仓参考行(P1-2): 已持仓时后端返回, 含成本/浮盈参考文案 */
  position?: {
    cost_price?: number | null
    quantity?: number | null
    last_close?: number | null
    pnl_pct?: number | null
    note?: string
  } | null
  /** 证据链(2026-10-10 证据化): 触发条件 / 数据时点 / 失效条件(反例检查)。旧响应无此字段 → 不渲染。 */
  evidence?: {
    triggers: string[]
    as_of: string | null
    as_of_is_today: boolean
    as_of_note: string
    invalidation: string[]
    invalidation_defaulted: boolean
  } | null
  /** 历史相似情形(从决策账本聚合); n<30 → insufficient, 不给百分比。旧响应无此字段 → 不渲染。 */
  similar?: {
    n: number
    up: number | null
    insufficient: boolean
    sentence: string
  } | null
}

type LoadState =
  | { status: 'loading' }
  | { status: 'error'; message: string }
  | { status: 'ready'; data: DecisionVerdict }

/** 三态徽标的语义色(动作语义, 非价格涨跌 —— 故走 gs/role 令牌, 不用 stock-up/down 文字色)。 */
const VERDICT_TONE: Record<string, string> = {
  动手: 'text-gs-go',
  看看: 'text-role-watch',
  别碰: 'text-gs-stop',
}

const KNOWN_VERDICTS = new Set(Object.keys(VERDICT_TONE))

export default function DecisionVerdictCard({ symbol, market }: { symbol: string; market: string }) {
  const [state, setState] = useState<LoadState>({ status: 'loading' })
  /** 竞态守卫取号器: 只认最新一次取数的结果(见文件头「竞态与去重」)。 */
  const seqRef = useRef(0)

  useEffect(() => {
    // 换标的/换市场先回到 loading —— 不留上一只票的结论(即使新响应悬挂不落地)。
    const seq = ++seqRef.current
    let alive = true
    setState({ status: 'loading' })
    insightApi
      .decision<DecisionVerdict>(symbol, market)
      .then((d) => {
        if (!alive || seq !== seqRef.current) return
        setState({ status: 'ready', data: d ?? {} })
      })
      .catch((e: unknown) => {
        if (!alive || seq !== seqRef.current) return
        setState({ status: 'error', message: e instanceof Error ? e.message : String(e) })
      })
    return () => {
      // 卸载/换标的: 弃掉在途响应(过期号同样被 seq 守卫拦下)
      alive = false
    }
    // 取数只依赖 symbol/market: 父组件无关状态重渲染不重发(见文件头)。
  }, [symbol, market])

  if (state.status === 'loading') {
    return (
      <div data-testid="decision-loading" className="text-[11px] text-muted-foreground">
        决策合成加载中…
      </div>
    )
  }

  if (state.status === 'error') {
    return (
      <div data-testid="decision-error" className="text-[11px] text-muted-foreground">
        决策合成取数失败 — 暂无法给出「动手 / 看看 / 别碰」
        {state.message ? <span className="text-muted-foreground/70">({state.message})</span> : null}
      </div>
    )
  }

  const verdict = state.data.verdict
  // verdict 缺失/表外: 不猜方向(空态显式)
  if (!verdict || !KNOWN_VERDICTS.has(verdict)) {
    return (
      <div data-testid="decision-empty" className="text-[11px] text-muted-foreground">
        决策无数据(verdict 缺失或不在表内) — 不猜方向
      </div>
    )
  }

  const reason = state.data.reason
  const personalizationNote = state.data.personalization_note
  const positionNote = state.data.position?.note
  const twoDimension = state.data.basis === 'two-dimension'
  const fundNote = state.data.fund_note || '资金维无数据(非CN)'
  const evidence = state.data.evidence
  const similar = state.data.similar
  return (
    <>
      <div className="flex flex-wrap items-center gap-x-2 gap-y-1" data-testid="decision-verdict-card">
        <span
          data-testid="decision-badge"
          className={`inline-flex items-center rounded border border-border/60 px-1.5 py-0.5 text-[12px] font-semibold ${VERDICT_TONE[verdict]}`}
        >
          {verdict}
        </span>
        {reason ? (
          <span data-testid="decision-reason" className="text-[11px] text-muted-foreground" title={reason}>
            {reason}
          </span>
        ) : (
          // verdict 已有而 reason 缺失: 不编造理由(显式缺数据)
          <span data-testid="decision-reason-missing" className="text-[11px] text-muted-foreground">
            理由无数据
          </span>
        )}
      </div>
      {/* P2-7: 非 CN 资金维无源 —— 显式标注, 不冒充三信号 */}
      {twoDimension ? (
        <span data-testid="decision-fund-missing" className="mt-0.5 block text-[10px] text-muted-foreground/70">
          {fundNote} — 仅趋势 × 活跃度双维判定
        </span>
      ) : null}
      {/* P1-2: 个性化注记(哪部分因个性化调整, 可解释) */}
      {personalizationNote ? (
        <span data-testid="decision-personalization" className="mt-0.5 block text-[10px] text-muted-foreground">
          个性化调整：{personalizationNote}
        </span>
      ) : null}
      {/* P1-2: 持仓参考行(不改 verdict 语义) */}
      {positionNote ? (
        <span data-testid="decision-position" className="mt-0.5 block text-[10px] text-muted-foreground/80">
          {positionNote}
        </span>
      ) : null}
      {/* 证据化(2026-10-10): 证据链/失效条件/相似情形 —— 仅追加, 旧响应无字段则不渲染 */}
      {evidence ? (
        <div
          data-testid="decision-evidence"
          className="mt-1 space-y-0.5 border-t border-border/40 pt-1 text-[10px]"
        >
          <div className="text-foreground/70" data-testid="decision-evidence-triggers">
            触发条件: {evidence.triggers.join('；')}
          </div>
          <div className="text-muted-foreground/70" data-testid="decision-evidence-asof">
            {evidence.as_of_note}
          </div>
          <div className="text-amber-600 dark:text-amber-500" data-testid="decision-evidence-invalidation">
            失效条件: {evidence.invalidation.join('；')}
          </div>
          {similar?.sentence ? (
            <div className="text-muted-foreground/70" data-testid="decision-evidence-similar">
              {similar.sentence}
            </div>
          ) : null}
        </div>
      ) : null}
    </>
  )
}
