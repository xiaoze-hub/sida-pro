import { useCallback, useEffect, useState } from 'react'
import { Navigate, Link } from 'react-router-dom'
import { Button } from '@panwatch/base-ui/components/ui/button'
import PageHeader from '@/components/PageHeader'
import { loadQuoteUniverse, preferredQuote, quotePath } from '@/lib/quote-universe'

export default function QuoteEntryPage() {
  const [target, setTarget] = useState('')
  const [error, setError] = useState('')
  const load = useCallback(async (current: () => boolean) => {
    setError('')
    const universe = await loadQuoteUniverse()
    if (!current()) return
    const selected = preferredQuote(universe)
    if (selected) setTarget(quotePath(selected))
    else if (universe.errors.length) setError(universe.errors.join('；'))
    else setTarget('/stocks/000001?type=index&default=1')
  }, [])
  useEffect(() => { let alive = true; void load(() => alive); return () => { alive = false } }, [load])
  if (target) return <Navigate to={target} replace />
  return <div className="page-container"><PageHeader title="行情" description="优先打开上次查看的持仓或关注股票，未选择时先看持仓；都没有时显示市场指数。" /><div className="card-surface rounded-xl bg-card p-5 text-[14px]">{error ? <><p role="alert">{error}</p><div className="mt-4 flex gap-2"><Button onClick={() => void load(() => true)}>重新读取</Button><Button variant="outline" asChild><Link to="/stocks/000001?type=index">查看市场指数</Link></Button></div></> : '正在读取你的持仓与关注列表…'}</div></div>
}
