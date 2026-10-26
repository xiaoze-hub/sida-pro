import { useRef, useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { Check, ChevronsUpDown, Search } from 'lucide-react'
import { Button } from '@panwatch/base-ui/components/ui/button'
import { Popover, PopoverContent, PopoverTrigger } from '@panwatch/base-ui/components/ui/popover'
import { loadQuoteUniverse, quoteChoiceKey, quotePath, rememberQuote, type QuoteUniverse } from '@/lib/quote-universe'

export function QuoteSymbolPicker({ symbol, market, type, defaultIndex }: { symbol: string; market: string; type: string; defaultIndex: boolean }) {
  const [open, setOpen] = useState(false)
  const [query, setQuery] = useState('')
  const [data, setData] = useState<QuoteUniverse | null>(null)
  const [loading, setLoading] = useState(false)
  const sequence = useRef(0)
  const navigate = useNavigate()
  const load = async () => {
    const request = ++sequence.current
    setLoading(true)
    const next = await loadQuoteUniverse()
    if (request === sequence.current) { setData(next); setLoading(false) }
  }
  const selected = `${market}:${symbol}`
  return <div className="flex flex-wrap items-center justify-between gap-2 rounded-xl bg-card px-4 py-3">
    <div className="flex flex-wrap items-center gap-2"><span className="text-[14px] font-medium">行情标的</span><span className="text-[12px] text-muted-foreground">{defaultIndex ? '暂无持仓或关注股票，当前显示默认上证指数' : type === 'stock' ? `${market} · ${symbol}` : type === 'index' ? '指数视图' : '板块视图'}</span></div>
    <Popover open={open} onOpenChange={value => { setOpen(value); if (value) void load() }}><PopoverTrigger asChild><Button variant="outline" size="sm" data-testid="quote-symbol-picker">切换持仓 / 关注股<ChevronsUpDown className="h-4 w-4" /></Button></PopoverTrigger>
      <PopoverContent align="end" className="w-80 max-w-[calc(100vw-2rem)] p-3">
        <label className="mb-3 flex items-center gap-2 rounded-lg border border-input bg-background px-3"><Search className="h-4 w-4 text-muted-foreground" /><input aria-label="搜索持仓与关注股票" value={query} onChange={event => setQuery(event.target.value)} placeholder="名称、代码或市场" className="h-10 min-w-0 flex-1 bg-transparent text-[14px] outline-none" /></label>
        {loading && <p role="status" className="mb-2 text-[12px] text-muted-foreground">正在更新股票列表…</p>}
        {data?.errors.length ? <p role="alert" className="mb-2 text-[12px] text-destructive">{data.errors.join('；')}，可重新打开重试。</p> : null}
        <div className="max-h-72 overflow-y-auto">{(['positions', 'watchlist'] as const).map(group => {
          const items = data?.[group].filter(item => `${item.name} ${item.symbol} ${item.market}`.toLowerCase().includes(query.toLowerCase())) ?? []
          return <div key={group} className="mb-3"><h3 className="mb-1 px-2 text-[12px] font-medium text-muted-foreground">{group === 'positions' ? '我的持仓' : '关注股票'}</h3>{items.length ? items.map(item => <button type="button" key={quoteChoiceKey(item)} onClick={() => { rememberQuote(item); setOpen(false); navigate(quotePath(item)) }} className={`flex min-h-11 w-full items-center justify-between gap-3 rounded-lg px-2 text-left text-[14px] hover:bg-s2 ${type === 'stock' && selected === quoteChoiceKey(item) ? 'bg-s3' : ''}`}><span className="min-w-0"><span className="block truncate">{item.name}</span><span className="font-mono text-[12px] text-muted-foreground">{item.market} · {item.symbol}</span></span>{type === 'stock' && selected === quoteChoiceKey(item) && <Check className="h-4 w-4 shrink-0 text-primary" />}</button>) : <p className="px-2 py-2 text-[12px] text-muted-foreground">{loading && !data ? '读取中…' : data?.errors.length ? '列表未完整读取' : query ? '没有匹配股票' : group === 'positions' ? '暂无持仓' : '暂无关注股票'}</p>}</div>
        })}</div>
        <div className="flex items-center justify-between border-t border-border pt-2"><Button variant="ghost" size="sm" asChild><Link to="/portfolio" onClick={() => setOpen(false)}>管理股票列表</Link></Button><Button variant="ghost" size="sm" onClick={() => { setOpen(false); navigate('/stocks/000001?type=index') }}>上证指数</Button></div>
      </PopoverContent>
    </Popover>
  </div>
}
