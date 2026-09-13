import { useCallback, useEffect, useRef } from 'react'
import { stocksApi } from '@panwatch/api'
import { useToast } from '@panwatch/base-ui/components/ui/toast'
import type { StockInsightModalProps } from './types'
import { isResourceEnabled, type ResourceKey } from './useInsightData'
import type { useInsightData } from './useInsightData'
import type { useInsightDerived } from './useInsightDerived'

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
 * 手工触发「盘中监测」(恢复组件的「一键设提醒」按钮)。派生自 `intraday_monitor` 的实际链路:
 * `stocksApi.list()` → 未关注则 `create()` → `updateAgents` 确保绑定 → `triggerAgent(..., 'intraday_monitor')`。
 *
 * **返回值**(Task 12 增量): `true` = 作业已提交(5s 轮询在跑), `false` = 未提交(已 toast 原始错误)。
 * 既有调用方(`InsightHeaderBar` 的 `onClick`)全部忽略返回值 ⇒ 行为逐字不变; 新增的消费方
 * (工作台「建议」标签)据此渲染失败提示 —— **不猜**失败原因。
 */
const handleSetAlert = async (): Promise<boolean> => {
  if (!symbol) return false
  setAlerting(true)
  try {
    const stocks = await stocksApi.list()
    let stock = (stocks || []).find(s => s.symbol === symbol && s.market === market) || null
    if (!stock) {
      stock = await stocksApi.create({ symbol, name: resolvedName || symbol, market })
    }

    const existingAgents = (stock.agents || []).map(a => ({
      agent_name: a.agent_name,
      schedule: a.schedule || '',
      ai_model_id: a.ai_model_id ?? null,
      notify_channel_ids: a.notify_channel_ids || [],
    }))
    const hasIntraday = existingAgents.some(a => a.agent_name === 'intraday_monitor')
    const nextAgents = hasIntraday
      ? existingAgents
      : [...existingAgents, { agent_name: 'intraday_monitor', schedule: '', ai_model_id: null, notify_channel_ids: [] }]

    await stocksApi.updateAgents(stock.id, { agents: nextAgents })
    await stocksApi.triggerAgent(stock.id, 'intraday_monitor', {
      bypass_throttle: true,
      bypass_market_hours: true,
    })
    toast('已设置提醒，AI 分析已提交', 'success')
    // 轮询等待建议生成（最多 2 分钟，每 5 秒一次）
    // Task 12 修复(既有缺陷, 与 Task 10 Finding 2 同形): 原实现持有**局部** `poll` 句柄 + 125s
    // setTimeout 自停, **卸载时不清** —— 工作台切走「建议」标签后最长 ~2 分钟仍每 5s 打
    // `/suggestions`。改为复用同文件既有的句柄管理(autoPollRef/autoPollStopRef/stopAutoPolling)
    // + `mountedRef` 守卫: 卸载/被新一轮取代都会清; await 期间若已卸载则不再装轮询、不再 setState。
    if (!mountedRef.current) return true
    stopAutoPolling() // 被新一轮触发取代时先清上一轮(防叠加)
    const before = Date.now()
    autoPollRef.current = setInterval(async () => {
      if (Date.now() - before > 120_000) { stopAutoPolling(); return }
      await loadSuggestions()
    }, 5_000)
    await loadSuggestions()
    if (!mountedRef.current) return true
    // 到点自停(句柄记在 ref, 卸载/被取代时由 stopAutoPolling 一并清)
    autoPollStopRef.current = setTimeout(() => stopAutoPolling(), 125_000)
    return true
  } catch (e) {
    if (mountedRef.current) toast(e instanceof Error ? e.message : '设置提醒失败', 'error')
    return false
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
    handleSetAlert,
    toggleWatch,
    triggerAutoAiSuggestion,
    copyTextWithFallback,
  }
}
