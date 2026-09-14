import { useCallback, useEffect, useRef } from 'react'
import { stocksApi } from '@panwatch/api'
import { useToast } from '@panwatch/base-ui/components/ui/toast'
import type { StockInsightModalProps } from './types'
import { isResourceEnabled, type ResourceKey } from './useInsightData'
import type { useInsightData } from './useInsightData'
import type { useInsightDerived } from './useInsightDerived'

/**
 * `triggerIntradayOnce` 的结果(v0.6.0 遗留③): 该动作不做任何**前置**写入(不加自选、不绑 Agent),
 * 故只有"提交成功/未成功"两态。
 * 注: 这**不等于**"零副作用" —— 提交成功后那轮 Agent 运行本身仍会落运行记录与站内通知
 * (见 `triggerIntradayOnce` 头注的 ⚠️ 段)。
 * 调用方仍**不得**据此推断失败原因(错误原文由本 action 内部 toast; `symbol` 缺失的早退是静默的)。
 */
export interface TriggerOnceOutcome {
  /** `true` = 作业已提交(5s 轮询在跑); `false` = 未提交。 */
  ok: boolean
}

export function useInsightActions(
  props: StockInsightModalProps,
  data: ReturnType<typeof useInsightData>,
  derived: ReturnType<typeof useInsightDerived>,
  enabledKeys?: ReadonlySet<ResourceKey>,
) {
  const {
    symbol, market, resolvedName, quote, loadSuggestions, watchingStock, setWatchingStock, stockCacheRef,
    autoTriggeredRef, holdingLoaded, holdingLoadError, autoSuggesting, setAutoSuggesting,
    setAlerting, setImageExporting, setWatchToggleLoading,
  } = data
  const { hasHolding, shareCardPayload, shareText, stockColors } = derived
  const { toast } = useToast()

// Task 10 修复(既存缺陷): 自动 AI 建议的 5s 轮询 interval 原先只在 125s 的 setTimeout 里清理,
// **卸载时不清** —— 离开标签后最长 2 分钟仍在打 `/suggestions`。改为显式持有句柄, 卸载/被取代时都清。
const autoPollRef = useRef<ReturnType<typeof setInterval> | null>(null)
const autoPollStopRef = useRef<ReturnType<typeof setTimeout> | null>(null)
// Task 10 复审修复: 卸载守卫。`autoPollRef`/`autoPollStopRef` 只在**卸载清理跑之前**被赋值才有效;
// 而 `triggerAutoAiSuggestion` 的 `await stocksApi.triggerAgent(...)` 期间宿主可能已卸载(切标签),
// 此时卸载清理早已跑过(refs 为 null), 续体若继续装 interval/stop-timer 就**没有任何东西会清**
// (最长 ~125s 空转打 `/suggestions`, 且是卸载后才发生的泄漏)。故每个 await 之后先查挂载态。
const mountedRef = useRef(true)
const stopAutoPolling = useCallback(() => {
  if (autoPollRef.current !== null) { clearInterval(autoPollRef.current); autoPollRef.current = null }
  if (autoPollStopRef.current !== null) { clearTimeout(autoPollStopRef.current); autoPollStopRef.current = null }
}, [])
const handleExportShareImage = useCallback(async () => {
  const esc = (s: string) => String(s || '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&apos;')
  const trim = (s: string, n = 42) => {
    const x = String(s || '')
    return x.length > n ? `${x.slice(0, n - 1)}…` : x
  }

  setImageExporting(true)
  try {
    const { marketLabel, price, chg, action, signal, reason, risks, technicalBrief, levelsBrief, source, ts } = shareCardPayload
    const up = (quote?.change_pct || 0) >= 0
    const changeColor = up ? stockColors.up : stockColors.down
    const svg = `
<svg xmlns="http://www.w3.org/2000/svg" width="1200" height="630" viewBox="0 0 1200 630">
<defs>
  <linearGradient id="bg" x1="0" y1="0" x2="1" y2="1">
    <stop offset="0%" stop-color="#0b1220"/>
    <stop offset="100%" stop-color="#111827"/>
  </linearGradient>
</defs>
<rect x="0" y="0" width="1200" height="630" fill="url(#bg)"/>
<rect x="40" y="30" width="1120" height="570" rx="22" fill="#0f172a" stroke="#1f2937"/>
<text x="76" y="104" fill="#93c5fd" font-size="26" font-family="-apple-system,BlinkMacSystemFont,Segoe UI,Microsoft YaHei,sans-serif">PanWatch 洞察</text>
<text x="76" y="150" fill="#f8fafc" font-size="42" font-weight="700" font-family="-apple-system,BlinkMacSystemFont,Segoe UI,Microsoft YaHei,sans-serif">${esc(trim(`${resolvedName}（${symbol} · ${marketLabel}）`, 28))}</text>
<text x="76" y="198" fill="#94a3b8" font-size="22" font-family="-apple-system,BlinkMacSystemFont,Segoe UI,Microsoft YaHei,sans-serif">${esc(ts)}</text>

<text x="76" y="284" fill="#94a3b8" font-size="24" font-family="-apple-system,BlinkMacSystemFont,Segoe UI,Microsoft YaHei,sans-serif">现价</text>
<text x="180" y="284" fill="#f8fafc" font-size="52" font-weight="700" font-family="-apple-system,BlinkMacSystemFont,Segoe UI,Microsoft YaHei,sans-serif">${esc(price)}</text>
<text x="380" y="284" fill="${changeColor}" font-size="36" font-weight="700" font-family="-apple-system,BlinkMacSystemFont,Segoe UI,Microsoft YaHei,sans-serif">${esc(chg)}</text>

<text x="76" y="352" fill="#94a3b8" font-size="24" font-family="-apple-system,BlinkMacSystemFont,Segoe UI,Microsoft YaHei,sans-serif">建议</text>
<text x="180" y="352" fill="#22d3ee" font-size="34" font-weight="700" font-family="-apple-system,BlinkMacSystemFont,Segoe UI,Microsoft YaHei,sans-serif">${esc(trim(action, 20))}</text>

<text x="76" y="412" fill="#94a3b8" font-size="24" font-family="-apple-system,BlinkMacSystemFont,Segoe UI,Microsoft YaHei,sans-serif">信号</text>
<text x="180" y="412" fill="#e2e8f0" font-size="26" font-family="-apple-system,BlinkMacSystemFont,Segoe UI,Microsoft YaHei,sans-serif">${esc(trim(signal, 46))}</text>

<text x="76" y="466" fill="#94a3b8" font-size="24" font-family="-apple-system,BlinkMacSystemFont,Segoe UI,Microsoft YaHei,sans-serif">理由</text>
<text x="180" y="466" fill="#cbd5e1" font-size="24" font-family="-apple-system,BlinkMacSystemFont,Segoe UI,Microsoft YaHei,sans-serif">${esc(trim(reason, 52))}</text>

<text x="76" y="520" fill="#94a3b8" font-size="24" font-family="-apple-system,BlinkMacSystemFont,Segoe UI,Microsoft YaHei,sans-serif">风险</text>
<text x="180" y="520" fill="#cbd5e1" font-size="24" font-family="-apple-system,BlinkMacSystemFont,Segoe UI,Microsoft YaHei,sans-serif">${esc(trim(risks, 52))}</text>

<text x="76" y="560" fill="#94a3b8" font-size="22" font-family="-apple-system,BlinkMacSystemFont,Segoe UI,Microsoft YaHei,sans-serif">技术</text>
<text x="180" y="560" fill="#cbd5e1" font-size="21" font-family="-apple-system,BlinkMacSystemFont,Segoe UI,Microsoft YaHei,sans-serif">${esc(trim(technicalBrief, 58))}</text>
<text x="76" y="590" fill="#94a3b8" font-size="22" font-family="-apple-system,BlinkMacSystemFont,Segoe UI,Microsoft YaHei,sans-serif">关键位</text>
<text x="180" y="590" fill="#cbd5e1" font-size="21" font-family="-apple-system,BlinkMacSystemFont,Segoe UI,Microsoft YaHei,sans-serif">${esc(trim(levelsBrief, 58))}</text>
<text x="76" y="618" fill="#64748b" font-size="18" font-family="-apple-system,BlinkMacSystemFont,Segoe UI,Microsoft YaHei,sans-serif">来源：${esc(source)} · 仅供参考，不构成投资建议</text>
</svg>`

    const blob = new Blob([svg], { type: 'image/svg+xml;charset=utf-8' })
    const url = URL.createObjectURL(blob)
    const img = await new Promise<HTMLImageElement>((resolve, reject) => {
      const el = new Image()
      el.onload = () => resolve(el)
      el.onerror = reject
      el.src = url
    })
    const canvas = document.createElement('canvas')
    canvas.width = 1200
    canvas.height = 630
    const ctx = canvas.getContext('2d')
    if (!ctx) throw new Error('无法创建画布')
    ctx.drawImage(img, 0, 0)
    URL.revokeObjectURL(url)
    const png = canvas.toDataURL('image/png')
    const a = document.createElement('a')
    a.href = png
    a.download = `panwatch-${symbol}-${Date.now()}.png`
    a.click()
    toast('分享图片已生成并下载', 'success')
  } catch {
    toast('图片生成失败，请稍后重试', 'error')
  } finally {
    setImageExporting(false)
  }
}, [quote?.change_pct, resolvedName, shareCardPayload, symbol, toast, stockColors.up, stockColors.down, setImageExporting])

const copyTextWithFallback = useCallback(async (text: string): Promise<boolean> => {
  if (!text) return false

  if (typeof navigator !== 'undefined' && navigator.clipboard?.writeText) {
    try {
      await navigator.clipboard.writeText(text)
      return true
    } catch {
      // Fallback to legacy copy below.
    }
  }

  if (typeof document !== 'undefined') {
    const textarea = document.createElement('textarea')
    textarea.value = text
    textarea.setAttribute('readonly', '')
    textarea.style.position = 'fixed'
    textarea.style.opacity = '0'
    textarea.style.pointerEvents = 'none'
    textarea.style.left = '-9999px'
    document.body.appendChild(textarea)
    try {
      textarea.focus()
      textarea.select()
      textarea.setSelectionRange(0, textarea.value.length)
      return !!document.execCommand?.('copy')
    } catch {
      return false
    } finally {
      document.body.removeChild(textarea)
    }
  }
  return false
}, [])

const handleCopyShareText = useCallback(async () => {
  try {
    const copied = await copyTextWithFallback(shareText)
    if (copied) {
      toast('洞察内容已复制', 'success')
    } else {
      toast('复制失败，请优先使用“图片”分享', 'error')
    }
  } catch {
    toast('复制失败，请优先使用“图片”分享', 'error')
  }
}, [copyTextWithFallback, shareText, toast])

const handleShareInsight = useCallback(async () => {
  try {
    if (typeof navigator !== 'undefined' && (navigator as any).share) {
      await (navigator as any).share({
        title: `${resolvedName} 洞察`,
        text: shareText,
      })
      return
    }
    const copied = await copyTextWithFallback(shareText)
    if (copied) {
      toast('当前环境不支持系统分享，已自动复制内容', 'success')
    } else {
      toast('当前环境不支持分享且复制失败，请使用“图片”分享', 'error')
    }
  } catch (e: any) {
    if (e?.name === 'AbortError') return
    const copied = await copyTextWithFallback(shareText)
    if (copied) {
      toast('分享失败，已自动复制内容', 'success')
    } else {
      toast('分享失败且复制失败，请使用“图片”分享', 'error')
    }
  }
}, [copyTextWithFallback, resolvedName, shareText, toast])

/**
 * **无自选/绑定副作用**地手工触发「盘中监测」(v0.6.0 遗留③: 工作台「建议」标签的按钮用本动作)。
 *
 * 直接 `triggerAgent(0, 'intraday_monitor', { allow_unbound: true, symbol, market, name })`
 * —— **不动自选、不动绑定**(不 `list`/不 `create`/不 `updateAgents`)。
 *
 * 后端证据(`src/web/api/stocks.py:461-533` `trigger_stock_agent`):
 *  - `stock_id <= 0` 时必须 `allow_unbound=true`, 否则 400;
 *  - 该标的**不在**当前用户自选时走"不落库"分支(`SimpleNamespace(id=0, …)`);
 *  - 已在自选时也只**读**既有 Stock/StockAgent 行, 不新建、不改绑定。
 *
 * ⚠️ **但这不是"零写入"** —— 一轮真实 Agent 运行本身就会落库:
 *  - `record_agent_run(...)` ⇒ 一条**运行记录**;
 *  - API 层收尾 `_notify` → `notify_task_done` ⇒ 一条**站内「任务完成」通知**
 *    (且不传 `user_id` 时兜底推给 owner)。
 *  - `suppress_notify = stock_id <= 0` 只影响**外发渠道**, **不等于**"不写站内通知"。
 *  ⇒ 措辞只能是"不加入自选、不绑定 Agent", **不得**写成"无任何持久化写入/不发通知"。
 *
 * KI-058(2026-09-18): 旧 `handleSetAlert`(list→create→updateAgents→trigger)已删 ——
 * v0.6.0 退役旧模态后它零生产调用方; 若将来要恢复「持久化设提醒」, 按产品设计重做入口,
 * 不要直接把本动作改回会偷偷写自选/绑定的路径。
 *
 * 与自动路径 `triggerAutoAiSuggestion` 的关系: 线格式(stock_id=0 + allow_unbound + symbol/market/name)
 * **完全相同**(它早已在用这条无绑定链路), 差别只在门控 —— 自动路径要过 `suggestions` 键、
 * "确认未持仓"、5 分钟去重与 `autoSuggesting`; 本动作是**用户点按钮**的一次性触发, 不套这些门控
 * (点了就该发), 但仍复用同一套 busy/轮询/卸载守卫:
 *  - busy: `setAlerting`(与「一键设提醒」共用一个禁用态, 避免两条路径同时提交);
 *  - 轮询: 复用 `autoPollRef`/`autoPollStopRef`/`stopAutoPolling` + `mountedRef`
 *    (Task 10/12 修的泄漏与在途卸载竞态, 本动作同样适用: await 之后先查挂载态);
 *  - 失败: toast 原始错误(不吞不伪装), 回传 `{ ok: false }`; **不猜**失败原因。
 *  早退: 无 `symbol`/`market` 时静默返回 `{ ok: false }`(与 `handleSetAlert` 同处置, 不 toast)。
 */
const triggerIntradayOnce = async (): Promise<TriggerOnceOutcome> => {
  if (!symbol || !market) return { ok: false }
  setAlerting(true)
  try {
    await stocksApi.triggerAgent(0, 'intraday_monitor', {
      allow_unbound: true,
      symbol,
      market,
      name: resolvedName || symbol,
      bypass_throttle: true,
      bypass_market_hours: true,
    })
    // 文案如实: 提交了一轮分析, 且**没有**发生自选/绑定写入(UI 不出现内部 agent 名/术语)。
    toast('已提交一轮盘中监测分析（一次性触发，未加入自选、未绑定提醒）', 'success')
    // 提交成功后轮询等新建议(最多 ~2 分钟, 每 5 秒一次) —— 句柄/卸载守卫与 handleSetAlert 同形。
    if (!mountedRef.current) return { ok: true }
    stopAutoPolling() // 被新一轮触发取代时先清上一轮(防叠加)
    const before = Date.now()
    autoPollRef.current = setInterval(async () => {
      if (Date.now() - before > 120_000) { stopAutoPolling(); return }
      await loadSuggestions()
    }, 5_000)
    await loadSuggestions()
    if (!mountedRef.current) return { ok: true }
    autoPollStopRef.current = setTimeout(() => stopAutoPolling(), 125_000)
    return { ok: true }
  } catch (e) {
    if (mountedRef.current) toast(e instanceof Error ? e.message : '触发盘中监测失败', 'error')
    return { ok: false }
  } finally {
    if (mountedRef.current) setAlerting(false)
  }
}

const toggleWatch = useCallback(async () => {
  if (!symbol) return
  if (watchingStock && hasHolding) {
    toast('该股票存在持仓，请先删除持仓后再取消关注', 'error')
    return
  }

  setWatchToggleLoading(true)
  try {
    if (watchingStock) {
      await stocksApi.remove(watchingStock.id)
      setWatchingStock(null)
      delete stockCacheRef.current[`${market}:${symbol}`]
      toast('已取消关注', 'success')
    } else {
      const created = await stocksApi.create({ symbol, name: resolvedName || symbol, market })
      setWatchingStock(created)
      stockCacheRef.current[`${market}:${symbol}`] = created
      toast('已添加关注', 'success')
    }
  } catch (e) {
    toast(e instanceof Error ? e.message : '操作失败', 'error')
  } finally {
    setWatchToggleLoading(false)
  }
}, [hasHolding, market, resolvedName, symbol, toast, watchingStock, setWatchToggleLoading, setWatchingStock, stockCacheRef])

const triggerAutoAiSuggestion = useCallback(async () => {
  // Task 10 门控(spec §4.3): 未启用 `suggestions` 键时**绝不触发** —— 本函数会真实提交后端 AI 作业
  // (`stocksApi.triggerAgent`), 默认路径(不传 enabledKeys)仍与旧行为逐字相同。
  if (!isResourceEnabled(enabledKeys, 'suggestions')) return
  // 自动建议仅针对”确认未持仓”的股票，且不自动创建股票/绑定 Agent。
  if (!symbol || !market || !holdingLoaded || holdingLoadError || hasHolding || autoSuggesting) return
  const key = `${market}:${symbol}`
  const lastTs = autoTriggeredRef.current[key] || 0
  if (Date.now() - lastTs < 5 * 60 * 1000) return
  autoTriggeredRef.current[key] = Date.now()
  setAutoSuggesting(true)
  try {
    // intraday_monitor 较 chart_analyst 更轻量、稳定，不依赖截图链路
    await stocksApi.triggerAgent(0, 'intraday_monitor', {
      allow_unbound: true,
      symbol,
      market,
      name: resolvedName || symbol,
      bypass_throttle: true,
      bypass_market_hours: true,
    })
    // 异步模式：triggerAgent 立即返回，轮询等待建议生成
    // Task 10 复审修复: await 期间组件可能已卸载 —— 此时续体**不得**再装轮询/自停定时器
    // (卸载清理已跑过, 没人会清这轮 interval), 也不再做 post-await setState。
    if (!mountedRef.current) return
    stopAutoPolling() // 被新一轮触发取代时先清掉上一轮(防叠加)
    const before = Date.now()
    autoPollRef.current = setInterval(async () => {
      if (Date.now() - before > 120_000) { stopAutoPolling(); setAutoSuggesting(false); return }
      await loadSuggestions()
    }, 5_000)
    await loadSuggestions()
    // 到点自停(句柄记在 ref, 卸载/被取代时由 stopAutoPolling 一并清)
    if (!mountedRef.current) return
    autoPollStopRef.current = setTimeout(() => stopAutoPolling(), 125_000)
    return
  } catch (e) {
    if (!mountedRef.current) return
    toast(
      e instanceof Error ? e.message : '自动 AI 建议触发失败，可点击「一键设提醒」重试',
      'error'
    )
    setAutoSuggesting(false)
  }
}, [symbol, market, resolvedName, holdingLoaded, holdingLoadError, hasHolding, autoSuggesting, enabledKeys, loadSuggestions, toast, autoTriggeredRef, setAutoSuggesting, stopAutoPolling])

useEffect(() => {
  if (!props.open || !symbol) return
  // Task 10 门控: 未启用 `suggestions` 时连 700ms 的自动触发定时器都不设。
  if (!isResourceEnabled(enabledKeys, 'suggestions')) return
  const timer = setTimeout(() => {
    triggerAutoAiSuggestion().catch(() => undefined)
  }, 700)
  return () => clearTimeout(timer)
}, [props.open, symbol, market, enabledKeys, triggerAutoAiSuggestion])

// Task 10 修复: 卸载(切走标签)时清掉 5s 轮询, 不留后台请求。
// Task 10 复审修复: 同时把 `mountedRef` 置 false —— 让 `triggerAutoAiSuggestion` 在 `await`
// 期间被卸载时(续体晚于本清理)能自查并放弃装轮询/自停定时器。body 里重置为 true 以兼容
// React 18 StrictMode 的「挂载→清理→再挂载」双跑(否则第二次挂载后被误判为已卸载)。
useEffect(() => {
  mountedRef.current = true
  return () => {
    mountedRef.current = false
    stopAutoPolling()
  }
}, [stopAutoPolling])


  return {
    handleExportShareImage,
    handleCopyShareText,
    handleShareInsight,
    // KI-058: 旧 handleSetAlert(持久化设提醒)已删(零生产调用方); 恢复能力需产品重做入口。
    triggerIntradayOnce,
    toggleWatch,
    triggerAutoAiSuggestion,
    copyTextWithFallback,
  }
}
