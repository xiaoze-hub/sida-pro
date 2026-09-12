/** K线副图注册表(v0.5.78, 借鉴 tick-stock-panel 的"子图注册表 + 每 pane 信息栏")。
 *
 * 为什么要有这个文件: 副图的**名字、口径提示、pane 顺序、悬停读数**原先散在
 * `InteractiveKline.tsx` 的 1200 行里(MACD 的 label 在 tooltip、RSI 的开关在按钮文案、
 * 成交量单位在别处), 加一个副图要改三处且容易口径不一致。这里收成一份声明,
 * 组件与测试都只读它。
 *
 * 读数格式化与图形绘制无关 —— 保持纯函数, 才能不靠浏览器就验证"信息栏显示什么"。
 */
import { safeFixed } from '@/lib/format'

export type SubChartKey = 'vol' | 'macd' | 'rsi'

/** 悬停/最新一根 K 线上, 副图信息栏要读的字段(缺字段一律 null, 不补 0)。 */
export interface SubChartRow {
  date: string
  volume: number | null
  volMa5: number | null
  volMa10: number | null
  macd: number | null
  signal: number | null
  hist: number | null
  rsi6: number | null
}

export interface SubChartReadout {
  label: string
  value: string
  /** 涨跌语义色: 只用于"多头/空头"这类方向读数, 遵循全仓"红绿只给价格"的约束。 */
  tone?: 'up' | 'down' | 'neutral'
}

export interface SubChartDef {
  key: SubChartKey
  /** 信息栏标题(含参数口径, 让读数可复核) */
  label: string
  /** 该 pane 在图里的顺序: 0=主图, 1=成交量, 2=MACD, 3=RSI */
  paneIndex: number
  /** 该副图是否画出来了: RSI 受"强弱线"开关控制, 关掉就不该出现在信息栏里 */
  visible?: boolean
  readouts(row: SubChartRow): SubChartReadout[]
}

/** 万手: 与主图指标带同一换算(原始单位=手)。 */
function wanShou(v: number | null): string {
  return v == null ? '--' : `${safeFixed(v / 10000, 1)}万手`
}

export const SUBCHARTS: SubChartDef[] = [
  {
    key: 'vol',
    label: '成交量',
    paneIndex: 1,
    readouts: (r) => [
      { label: '量', value: wanShou(r.volume) },
      { label: 'MA5', value: wanShou(r.volMa5) },
      { label: 'MA10', value: wanShou(r.volMa10) },
    ],
  },
  {
    key: 'macd',
    label: 'MACD(12,26,9)',
    paneIndex: 2,
    readouts: (r) => [
      { label: 'DIF', value: r.macd == null ? '--' : safeFixed(r.macd, 3) },
      { label: 'DEA', value: r.signal == null ? '--' : safeFixed(r.signal, 3) },
      {
        label: '柱',
        value: r.hist == null ? '--' : safeFixed(r.hist, 3),
        tone: r.hist == null ? 'neutral' : r.hist >= 0 ? 'up' : 'down',
      },
    ],
  },
  {
    key: 'rsi',
    label: 'RSI(6)',
    paneIndex: 3,
    readouts: (r) => [
      { label: '强弱', value: r.rsi6 == null ? '--' : safeFixed(r.rsi6, 1) },
      {
        label: '区',
        value: r.rsi6 == null ? '--' : r.rsi6 >= 70 ? '超买' : r.rsi6 <= 30 ? '超卖' : '中性',
      },
    ],
  },
]

export const subChartOf = (key: SubChartKey): SubChartDef =>
  SUBCHARTS.find((s) => s.key === key) as SubChartDef

/** 信息栏要显示的副图: 按 pane 顺序, 且只保留开了的(RSI 关掉就不该挂一条空栏)。 */
export function visibleSubCharts(hidden: Partial<Record<SubChartKey, boolean>> = {}): SubChartDef[] {
  return SUBCHARTS.filter((s) => !hidden[s.key]).sort((a, b) => a.paneIndex - b.paneIndex)
}

/** 一条副图信息栏的最终文本(空 row / 未测量一律 '--', 不补 0)。 */
export function subChartReadouts(def: SubChartDef, row: SubChartRow | null): SubChartReadout[] {
  if (!row) return def.readouts({ date: '', volume: null, volMa5: null, volMa10: null, macd: null, signal: null, hist: null, rsi6: null })
  return def.readouts(row)
}
