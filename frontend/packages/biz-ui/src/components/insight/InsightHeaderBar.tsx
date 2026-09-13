import { useInsight } from './context'
import StockPriceAlertPanel from '@panwatch/biz-ui/components/stock-price-alert-panel'
import type { InsightTab } from './types'
import { Button } from '@panwatch/base-ui/components/ui/button'
import { Copy } from 'lucide-react'
import { DialogDescription } from '@panwatch/base-ui/components/ui/dialog'
import { DialogHeader } from '@panwatch/base-ui/components/ui/dialog'
import { DialogTitle } from '@panwatch/base-ui/components/ui/dialog'
import { Download } from 'lucide-react'
import { ExternalLink } from 'lucide-react'
import { RefreshCw } from 'lucide-react'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@panwatch/base-ui/components/ui/select'
import { Share2 } from 'lucide-react'
import { Sparkles } from 'lucide-react'
import { Switch } from '@panwatch/base-ui/components/ui/switch'

export function InsightHeaderBar() {
  const {
    symbol,
    goFullQuote,
    market,
    loading,
    tab,
    setTab,
    autoRefreshEnabled,
    setAutoRefreshEnabled,
    autoRefreshSec,
    setAutoRefreshSec,
    suggestions,
    news,
    announcements,
    reports,
    deepResult,
    alerting,
    watchingStock,
    watchToggleLoading,
    imageExporting,
    resolvedName,
    handleRefreshAll,
    hasHolding,
    buildPageContext,
    badge,
    handleExportShareImage,
    handleCopyShareText,
    handleShareInsight,
    handleSetAlert,
    toggleWatch,
    props,
  } = useInsight()
  return (
    <>
    <DialogHeader className="mb-3">
      <div className="flex items-start justify-between gap-3 pr-10 md:pr-8">
        <div className="shrink-0">
          <DialogTitle className="flex items-center gap-2 flex-wrap">
            <span className={`text-[10px] px-2 py-0.5 rounded ${badge.style}`}>{badge.label}</span>
            <span className="break-all">{resolvedName}</span>
            <span className="font-mono text-[12px] text-muted-foreground">({symbol})</span>
          </DialogTitle>
          <DialogDescription className="hidden md:block">概览、K线、AI建议、新闻、历史分析都在同一弹窗查看</DialogDescription>
        </div>
        <div className="hidden md:flex items-center gap-2">
          <Button variant="secondary" size="sm" className="h-8 px-2.5" onClick={() => handleExportShareImage()} disabled={imageExporting}>
            <Download className={`w-3.5 h-3.5 ${imageExporting ? 'animate-pulse' : ''}`} />
            <span>{imageExporting ? '生成中' : '图片'}</span>
          </Button>
          <Button variant="secondary" size="sm" className="h-8 px-2.5" onClick={() => handleShareInsight()}>
            <Share2 className="w-3.5 h-3.5" />
            <span>分享</span>
          </Button>
          <Button variant="secondary" size="sm" className="h-8 px-2.5" onClick={() => handleCopyShareText()}>
            <Copy className="w-3.5 h-3.5" />
            <span>复制</span>
          </Button>
          <Button
            variant="secondary"
            size="sm"
            className="h-8 px-2.5"
            onClick={toggleWatch}
            disabled={watchToggleLoading || (hasHolding && !!watchingStock)}
            title={hasHolding && watchingStock ? '持仓中的股票无法取消关注' : undefined}
          >
            {watchToggleLoading ? '处理中...' : (watchingStock ? (hasHolding ? '持仓中' : '取消关注') : '快速关注')}
          </Button>
          <StockPriceAlertPanel mode="inline" symbol={symbol} market={market} stockName={resolvedName} />
          <Button variant="secondary" size="sm" className="h-8 px-2.5" onClick={handleSetAlert} disabled={alerting}>
            {alerting ? '设置中...' : '一键设提醒'}
          </Button>
          <Button
            variant="secondary"
            size="sm"
            className="h-8 px-2.5"
            onClick={() => {
              window.dispatchEvent(new CustomEvent('panwatch-open-chat', {
                detail: { symbol, market, stockName: resolvedName, pageContext: buildPageContext() }
              }))
              props.onOpenChange(false)
            }}
          >
            <Sparkles className="w-3.5 h-3.5 mr-1" /> 问 AI
          </Button>
          <Button variant="secondary" size="sm" className="h-8 px-2.5" onClick={goFullQuote} title="在全屏行情页查看">
            <ExternalLink className="w-3.5 h-3.5" />
            <span>全屏行情</span>
          </Button>
          <Button variant="outline" size="sm" className="h-8 px-2.5" onClick={() => handleRefreshAll()} disabled={loading}>
            <RefreshCw className={`w-3.5 h-3.5 ${loading ? 'animate-spin' : ''}`} />
          </Button>
        </div>
      </div>
      <div className="flex md:hidden items-center gap-2 mt-2 overflow-x-auto scrollbar-none pb-1 -mb-1">
        <Button variant="secondary" size="sm" className="h-8 px-2.5 shrink-0" onClick={() => handleExportShareImage()} disabled={imageExporting}>
          <Download className={`w-3.5 h-3.5 ${imageExporting ? 'animate-pulse' : ''}`} />
        </Button>
        <Button variant="secondary" size="sm" className="h-8 px-2.5 shrink-0" onClick={() => handleShareInsight()}>
          <Share2 className="w-3.5 h-3.5" />
        </Button>
        <Button variant="secondary" size="sm" className="h-8 px-2.5 shrink-0" onClick={() => handleCopyShareText()}>
          <Copy className="w-3.5 h-3.5" />
        </Button>
        <Button
          variant="secondary"
          size="sm"
          className="h-8 px-2.5 shrink-0"
          onClick={toggleWatch}
          disabled={watchToggleLoading || (hasHolding && !!watchingStock)}
        >
          {watchToggleLoading ? '处理中...' : (watchingStock ? (hasHolding ? '持仓中' : '取消关注') : '快速关注')}
        </Button>
        <StockPriceAlertPanel mode="inline" symbol={symbol} market={market} stockName={resolvedName} />
        <Button variant="secondary" size="sm" className="h-8 px-2.5 shrink-0" onClick={handleSetAlert} disabled={alerting}>
          {alerting ? '设置中...' : '一键设提醒'}
        </Button>
        <Button
          variant="secondary"
          size="sm"
          className="h-8 px-2.5 shrink-0"
          onClick={() => {
            window.dispatchEvent(new CustomEvent('panwatch-open-chat', {
              detail: { symbol, market, stockName: resolvedName, pageContext: buildPageContext() }
            }))
            props.onOpenChange(false)
          }}
        >
          <Sparkles className="w-3.5 h-3.5 mr-1" /> 问 AI
        </Button>
        <Button variant="secondary" size="sm" className="h-8 px-2.5 shrink-0" onClick={goFullQuote} title="在全屏行情页查看">
          <ExternalLink className="w-3.5 h-3.5" />
        </Button>
        <Button variant="outline" size="sm" className="h-8 px-2.5 shrink-0" onClick={() => handleRefreshAll()} disabled={loading}>
          <RefreshCw className={`w-3.5 h-3.5 ${loading ? 'animate-spin' : ''}`} />
        </Button>
      </div>
    </DialogHeader>

    <div className="flex items-center justify-between gap-2 flex-wrap mb-3">
      <div className="flex items-center gap-1 flex-wrap">
        {[
          { id: 'overview', label: '概览' },
          { id: 'suggestions', label: `建议 (${suggestions.length})` },
          { id: 'reports', label: `报告 (${reports.length})` },
          { id: 'deep', label: deepResult ? '深度 (1)' : '深度' },
          { id: 'kline', label: 'K线' },
          { id: 'fundamentals', label: '基本面' },
          { id: 'announcements', label: `公告 (${announcements.length})` },
          { id: 'news', label: `新闻 (${news.length})` },
          { id: 'company', label: '简介' },
        ].map(item => (
          <button
            key={item.id}
            onClick={() => setTab(item.id as InsightTab)}
            className={`text-[11px] px-2.5 py-1 rounded transition-colors ${
              tab === item.id ? 'bg-primary text-primary-foreground' : 'bg-accent/50 text-muted-foreground hover:bg-accent'
            }`}
          >
            {item.label}
          </button>
        ))}
      </div>
      <div className="flex items-center gap-2">
        <span className="text-[11px] text-muted-foreground">自动刷新</span>
        <Switch
          checked={autoRefreshEnabled}
          onCheckedChange={setAutoRefreshEnabled}
          aria-label="自动刷新"
        />
        <Select value={String(autoRefreshSec)} onValueChange={(v) => setAutoRefreshSec(Number(v))}>
          <SelectTrigger className="h-7 w-[84px] text-[11px]">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="10">10秒</SelectItem>
            <SelectItem value="20">20秒</SelectItem>
            <SelectItem value="30">30秒</SelectItem>
            <SelectItem value="60">60秒</SelectItem>
          </SelectContent>
        </Select>
      </div>
    </div>
    </>
  )
}
