import { useEffect, useRef, useState } from 'react'
import { Loader2, Swords } from 'lucide-react'
import { chatApi } from '@sida/api'
import SafeMarkdown from '@/components/SafeMarkdown'

/**
 * 空头司令挑刺（红蓝对抗压力测试）—— 创新 6（2026-10-24，P1）。
 *
 * 定位：**逆向风险情报备料**，不是交易指令 —— 强制假设「这笔交易已亏 15%」，
 * 让参谋站到空头立场给 3 个死因 + 证据链 + 可证伪的反驳条件。
 *
 * 触发链路（**复用现有参谋 API 路径，不新增端点**）：
 *   按钮 → `chatApi.createConversation({ stock_symbol, stock_market })`（现有 /chat/conversations）
 *        → `chatApi.sendMessageStream(conv.id, "空头司令挑刺（压力测试）{symbol}", …)`
 *          （现有 SSE 流式对话端点）。
 *   后端按问句关键词**确定性路由**到 harness 配方 `devil_advocate`（when:
 *   空头司令/挑刺/压力测试/红蓝对抗/死因/逆向风控，见 src/harness/recipes/devil_advocate.yaml），
 *   管线按固定顺序取数（三口径资金 + 筹码 + 消息面 + 情景矩阵，全部现有工具），模型只写结论。
 *   问句里带 6 位代码 → 管线 `symbols_of` 直接解析标的；不依赖会话绑定兜底。
 *
 * 已知副作用（如实披露，不伪装"零写入"）：
 *   - 会新建一条参谋会话并落库用户/AI 两条消息（AI 参谋页可见）；
 *   - 该轮运行会写 CopilotRun 轨迹（治理页可见）。
 *   - **不会发生任何交易委托**：本面板只渲染报告，配方提示词明令禁止买卖指令。
 *
 * 失败态如实陈述：展示后端 error 原文，不推断原因、不编造"已生成"。
 * 排版铁律：hairline 分隔（无卡片底、无 shadow-float）；字号 10/11/12；表格数字 tabular-nums；
 * 结论是否可证伪由配方提示词约束，本组件不二次加工 AI 正文（SafeMarkdown 原样渲染）。
 */

type Phase = 'idle' | 'running' | 'done' | 'failed'

/** 触发问句：含配方关键词（确定性路由）+ 6 位代码（管线解析标的）。 */
const devilPrompt = (symbol: string) => `空头司令挑刺（压力测试）${symbol}`

export function DevilAdvocatePanel({ symbol, market }: { symbol: string; market: string }) {
  const [phase, setPhase] = useState<Phase>('idle')
  const [stage, setStage] = useState('')
  const [result, setResult] = useState('')
  const [errMsg, setErrMsg] = useState('')
  const abortRef = useRef<AbortController | null>(null)
  const aliveRef = useRef(true)

  useEffect(() => {
    aliveRef.current = true
    return () => {
      aliveRef.current = false
      abortRef.current?.abort() // 卸载即断流（后端检测断开会中止工具循环，停止计费）
    }
  }, [])

  const running = phase === 'running'

  const onTrigger = () => {
    if (running) return
    setPhase('running')
    setResult('')
    setErrMsg('')
    setStage('正在发起压力测试…')
    void (async () => {
      try {
        const ctrl = new AbortController()
        abortRef.current = ctrl
        // 现有参谋触发路径：建会话 → 流式发消息（两个都是既有 /chat 端点）
        const conv = await chatApi.createConversation({
          stock_symbol: symbol,
          stock_market: market,
        })
        let streamSettled = false
        let acc = ''
        await chatApi.sendMessageStream(
          conv.id,
          devilPrompt(symbol),
          {
            onStage: (m) => {
              if (aliveRef.current) setStage(m)
            },
            onDelta: (c) => {
              acc += c
              if (!aliveRef.current) return
              setStage('')
              setResult((prev) => prev + c)
            },
            onDone: (msg) => {
              streamSettled = true
              if (!aliveRef.current) return
              setResult(String(msg.content || ''))
              setPhase('done')
            },
            onError: (m) => {
              streamSettled = true
              if (!aliveRef.current) return
              setErrMsg(String(m || ''))
              setPhase('failed')
            },
          },
          ctrl.signal,
          undefined,
          { symbol, market },
        )
        // 流结束但既没 done 也没 error（如连接被提前掐断）→ 如实按失败处理，不装成功
        if (!streamSettled && aliveRef.current) {
          if (acc.trim()) {
            setResult(acc)
            setPhase('done')
          } else {
            setErrMsg('')
            setPhase('failed')
          }
        }
      } catch (e) {
        if (!aliveRef.current) return
        setErrMsg(e instanceof Error ? e.message : String(e))
        setPhase('failed')
      }
    })()
  }

  return (
    <div data-testid="devil-advocate-panel" className="border-t border-border pt-3">
      {/* 触发行：警示红（danger 语义色板 --destructive，与买红卖绿的行情色板无关） */}
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
        <span className="inline-flex items-center gap-1 text-[12px] font-medium text-foreground">
          <Swords className="h-3.5 w-3.5 text-destructive" />
          红蓝对抗压力测试
        </span>
        <span className="text-[12px] text-muted-foreground">
          假设这笔交易已亏 15%，空头立场给 3 个死因 + 证据链 + 反驳条件
        </span>
        <button
          type="button"
          onClick={onTrigger}
          disabled={running}
          data-testid="devil-advocate-trigger"
          title="呼叫空头司令挑刺（压力测试）：走参谋对话接口对当前标的一轮逆向风控研判 —— 只出报告，不下单、不改持仓。会新建一条参谋会话（AI 参谋页可见）；证据缺失处将标注「无数据」，无现成工具的维度标注「无数据源」。"
          className="ml-auto inline-flex h-7 items-center gap-1 rounded border border-destructive/40 bg-destructive/10 px-2.5 text-[12px] font-medium text-destructive hover:bg-destructive/15 disabled:opacity-60"
        >
          {running ? <Loader2 className="h-3 w-3 animate-spin" /> : <Swords className="h-3 w-3" />}
          {running ? '空头司令研判中…' : '呼叫空头司令挑刺（压力测试）'}
        </button>
      </div>

      {/* 取数阶段提示（流式 stage 事件原文） */}
      {running && stage ? (
        <div className="mt-2 flex items-center gap-1.5 text-[12px] text-muted-foreground" data-testid="devil-advocate-stage">
          <Loader2 className="h-3 w-3 animate-spin" />
          {stage}
        </div>
      ) : null}

      {/* 结果：hairline 分隔，SafeMarkdown 原样渲染（GFM 表格/要点行），不做二次加工 */}
      {result ? (
        <div className="mt-2 border-t border-border pt-2" data-testid="devil-advocate-result">
          <div className="text-[12px] leading-relaxed [&_table]:tabular-nums">
            <SafeMarkdown>{result}</SafeMarkdown>
          </div>
          <div className="mt-2 border-t border-border pt-1.5 text-[12px] text-muted-foreground">
            逆向风险情报备料，非买卖指令；压力情景为假设性研判；「无数据」= 本轮证据缺失，「无数据源」= 平台暂无该维度工具。
          </div>
        </div>
      ) : null}

      {/* 失败态：展示后端 error 原文，不推断原因、不谎称已生成 */}
      {phase === 'failed' ? (
        <div className="mt-2 text-[12px] text-muted-foreground" data-testid="devil-advocate-failed">
          挑刺失败：本轮压力测试未完成 —— 可重试。
          {errMsg ? <span> 流中断原因（原文）：{errMsg}</span> : ' 流在给出结论前结束，未生成报告。'}
          {' 本次未发生任何交易委托。'}
        </div>
      ) : null}
    </div>
  )
}

export default DevilAdvocatePanel
