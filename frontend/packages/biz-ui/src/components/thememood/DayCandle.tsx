import { readStockColors } from '@panwatch/biz-ui/lib/stock-colors'

export interface DayCandleOHLC { o: number; h: number; l: number; c: number }

/** 单根日K(v0.5.85, spec §6): 红=收>=开, 绿=收<开; candle=null → 占位, 不编影线。 */
export default function DayCandle({ candle, basis }: { candle: DayCandleOHLC | null; basis: 'qfq' | 'raw' }) {
  if (!candle) {
    return <span title="无K数据" className="inline-block h-[26px] w-[10px] rounded-sm bg-muted/40" />
  }
  const sc = readStockColors()
  const up = candle.c >= candle.o
  const color = up ? sc.up : sc.down
  const hi = Math.max(candle.h, candle.o, candle.c)
  const lo = Math.min(candle.l, candle.o, candle.c)
  const span = hi - lo || 1
  const y = (v: number) => 25 - ((v - lo) / span) * 24
  const bodyTop = y(Math.max(candle.o, candle.c))
  const bodyH = Math.max(2, Math.abs(y(candle.o) - y(candle.c)))
  const basisLabel = basis === 'qfq' ? '前复权' : '盘中原始口径'
  return (
    <svg
      width={10}
      height={26}
      aria-label={up ? '阳柱' : '阴柱'}
    >
      <title>{`${basisLabel} 开${candle.o} 高${candle.h} 低${candle.l} 收${candle.c}`}</title>
      <line x1={5} x2={5} y1={y(candle.h)} y2={y(candle.l)} stroke={color} strokeWidth={1} />
      <rect x={1} y={bodyTop} width={8} height={bodyH} fill={color} />
    </svg>
  )
}
