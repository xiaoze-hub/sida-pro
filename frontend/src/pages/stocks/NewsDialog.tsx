import { Button } from '@panwatch/base-ui/components/ui/button'
import { Dialog } from '@panwatch/base-ui/components/ui/dialog'
import { DialogContent } from '@panwatch/base-ui/components/ui/dialog'
import { DialogDescription } from '@panwatch/base-ui/components/ui/dialog'
import { DialogHeader } from '@panwatch/base-ui/components/ui/dialog'
import { DialogTitle } from '@panwatch/base-ui/components/ui/dialog'
import { ExternalLink } from 'lucide-react'
import { Newspaper } from 'lucide-react'
import { RefreshCw } from 'lucide-react'
import { useStocks } from './context'

export function NewsDialog() {
  const {
    stocks,
    newsDialogOpen,
    setNewsDialogOpen,
    newsDialogSymbol,
    setNewsDialogSymbol,
    news,
    newsLoading,
    loadNews,
  } = useStocks()
  return (
    <>
{/* 相关资讯弹窗 */}
<Dialog open={newsDialogOpen} onOpenChange={setNewsDialogOpen}>
  <DialogContent className="max-w-2xl max-h-[85vh] flex flex-col">
    <DialogHeader>
      <DialogTitle className="flex items-center gap-2">
        <Newspaper className="w-5 h-5 text-blue-600" />
        相关资讯
      </DialogTitle>
      <DialogDescription>
        {newsDialogSymbol
          ? `${newsDialogSymbol} 的相关新闻和公告`
          : '自选股相关新闻和公告（近 72 小时）'
        }
      </DialogDescription>
    </DialogHeader>

    {/* 股票筛选器 */}
    <div className="flex items-center gap-2 flex-wrap py-2 border-b">
      <span className="text-[12px] text-muted-foreground">筛选:</span>
      <button
        onClick={() => { setNewsDialogSymbol(''); loadNews() }}
        className={`text-[11px] px-2.5 py-1 rounded-md transition-colors ${
          !newsDialogSymbol
            ? 'bg-primary text-primary-foreground'
            : 'bg-accent/50 text-muted-foreground hover:bg-accent'
        }`}
      >
        全部
      </button>
      {stocks.slice(0, 10).map(stock => (
        <button
          key={stock.symbol}
          onClick={() => { setNewsDialogSymbol(stock.name); loadNews(stock.name) }}
          className={`text-[11px] px-2.5 py-1 rounded-md transition-colors ${
            newsDialogSymbol === stock.name
              ? 'bg-primary text-primary-foreground'
              : 'bg-accent/50 text-muted-foreground hover:bg-accent'
          }`}
        >
          {stock.name}
        </button>
      ))}
      {stocks.length > 10 && (
        <span className="text-[10px] text-muted-foreground">+{stocks.length - 10}</span>
      )}
    </div>

    {/* 新闻列表 */}
    <div className="flex-1 overflow-y-auto min-h-0 py-2">
      {newsLoading ? (
        <div className="flex items-center justify-center py-12">
          <span className="w-5 h-5 border-2 border-primary/30 border-t-primary rounded-full animate-spin" />
          <span className="ml-2 text-[13px] text-muted-foreground">加载中...</span>
        </div>
      ) : news.length === 0 ? (
        <div className="text-center py-12 text-muted-foreground text-[13px]">
          暂无相关资讯
        </div>
      ) : (
        <div className="space-y-2">
          {news.map((item, idx) => (
            <div
              key={`${item.source}-${item.external_id}-${idx}`}
              className="border-b border-border/40 py-2.5 hover:bg-accent/20 transition-colors"
            >
              <div className="flex items-start justify-between gap-3">
                <div className="flex-1 min-w-0">
                  <div className="flex items-center gap-2 mb-1.5">
                    <span className={`text-[10px] px-1.5 py-0.5 rounded ${
                      item.source === 'eastmoney' ? 'bg-amber-500/10 text-amber-600 dark:text-amber-600' :
                      item.source === 'eastmoney_news' ? 'bg-blue-500/10 text-blue-600' :
                      'bg-emerald-500/10 text-emerald-600 dark:text-emerald-700'
                    }`}>
                      {item.source_label}
                    </span>
                    {item.importance >= 2 && (
                      <span className="text-[10px] px-1.5 py-0.5 rounded bg-rose-500/10 text-rose-600">
                        重要
                      </span>
                    )}
                    <span className="text-[10px] text-muted-foreground">
                      {item.publish_time}
                    </span>
                  </div>
                  <a
                    href={item.url}
                    target="_blank"
                    rel="noopener noreferrer"
                    className="text-[13px] font-medium text-foreground hover:text-primary transition-colors block"
                  >
                    {item.title}
                  </a>
                  {item.symbols.length > 0 && (
                    <div className="flex items-center gap-1.5 mt-2">
                      {item.symbols.slice(0, 5).map(sym => {
                        const stockInfo = stocks.find(s => s.symbol === sym)
                        const stockName = stockInfo?.name || sym
                        return (
                          <button
                            key={sym}
                            onClick={() => { setNewsDialogSymbol(stockName); loadNews(stockName) }}
                            className="text-[10px] px-1.5 py-0.5 rounded bg-primary/10 text-primary font-mono hover:bg-primary/20 transition-colors"
                          >
                            {stockName}
                          </button>
                        )
                      })}
                      {item.symbols.length > 5 && (
                        <span className="text-[10px] text-muted-foreground">+{item.symbols.length - 5}</span>
                      )}
                    </div>
                  )}
                </div>
                <a
                  href={item.url}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="flex-shrink-0 p-1.5 rounded-md hover:bg-accent transition-colors"
                  title="查看原文"
                >
                  <ExternalLink className="w-4 h-4 text-muted-foreground" />
                </a>
              </div>
            </div>
          ))}
        </div>
      )}
    </div>

    {/* 底部刷新按钮 */}
    <div className="flex items-center justify-between pt-2 border-t">
      <span className="text-[11px] text-muted-foreground">
        共 {news.length} 条资讯
      </span>
      <Button variant="secondary" size="sm" onClick={() => loadNews(newsDialogSymbol || undefined)} disabled={newsLoading}>
        <RefreshCw className={`w-3 h-3 ${newsLoading ? 'animate-spin' : ''}`} />
        刷新
      </Button>
    </div>
  </DialogContent>
</Dialog>
    </>
  )
}
