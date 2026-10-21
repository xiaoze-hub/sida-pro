import { cn } from '@/lib/utils'
import { normSymbol } from '@/lib/symbol'
import { useStockNames } from '@/hooks/useStockNames'

/**
 * 统一「名称 + 代码」展示(2026-10-21)。
 *
 * 背景: 决策账本明细 / 聚宝盆选股 / 策略应用结果 / 裁判战绩 / 行情页头部等多处只显示
 * 6 位代码, 用户看不出是哪只票。本组件是全站唯一展示口径, 规则三条:
 *
 *  ① 有名称 ⇒ **名称为主**、代码为辅(`font-mono text-[11px] text-muted-foreground`);
 *  ② 拿不到名称 ⇒ **只显示代码** —— 不写「未知」、不写「--」、不占位。诚实但克制:
 *     名称是锦上添花, 缺了不影响这一行还是可读的(代码本身是真信息);
 *  ③ 不新增卡片、不加边框、不做数据堆砌 —— 就是一串文字, 可塞进表格单元格/标题/徽标行。
 *
 * 名称来源优先级: `name` prop(后端响应自带) **>** `useStockNames` 缓存(批量补名)。
 * 数字/金额格式化**不在本组件内做**, 需要时走 `@/lib/format`(ui-rules R6 禁裸 .toFixed)。
 */
export interface StockLabelProps {
  /** 6 位代码(容忍 `USZA000006` / `600000.SH` / `sh600000` 等内部形态, 展示前归一化) */
  symbol: string
  /** 后端已给名称时直接传 —— 省一次批量补名, 且以调用方口径为准 */
  name?: string | null
  /** 市场; 缺省 CN。名称端点目前只覆盖 CN, 其它市场只用 `name` prop */
  market?: string
  /** 是否把代码作为次要信息一起显示(默认 true; 窄容器可关掉) */
  showCode?: boolean
  className?: string
}

export function StockLabel({
  symbol,
  name,
  market = 'CN',
  showCode = true,
  className,
}: StockLabelProps) {
  const code = normSymbol(symbol)
  // 非 CN 不走批量补名(端点只覆盖 A 股, 拿同号境外标的的名字会是错名字)
  const autoLookup = String(market).trim().toUpperCase() === 'CN'
  const names = useStockNames(autoLookup && code ? [code] : [])

  const resolved = (name ?? '').trim() || names[code]

  // 纪律 ②: 没有名称就只给代码 —— 不渲染任何占位文案
  if (!resolved) {
    return (
      <span
        data-testid="stock-label"
        data-resolved="false"
        className={cn('font-mono', className)}
      >
        {code}
      </span>
    )
  }

  return (
    <span
      data-testid="stock-label"
      data-resolved="true"
      className={cn('inline-flex min-w-0 items-baseline gap-1', className)}
    >
      <span className="truncate" title={resolved}>
        {resolved}
      </span>
      {showCode ? (
        <span className="font-mono text-[11px] text-muted-foreground">{code}</span>
      ) : null}
    </span>
  )
}

export default StockLabel
