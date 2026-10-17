import { useCallback } from 'react'
import { useNavigate } from 'react-router-dom'
import { Flame, Clock, ShieldCheck, Zap } from 'lucide-react'
import AuctionAnomalyTab from '@panwatch/biz-ui/components/AuctionAnomalyTab'
import { navBackState } from '@/lib/nav-back'

/**
 * 集合竞价专区 (Auction Page)
 * 专为 9:15 ~ 9:35 盘前打板与弱转强极速决策打造。
 * 融合通达信 TQ 盘口 (OpenAmo 竞价金额 / OpenVolPre1 竞价量比 / 撤单率) 与同花顺异动。
 */
export default function AuctionPage() {
  const navigate = useNavigate()

  const openAuctionDetail = useCallback(
    (symbol: string) => {
      navigate(`/stocks/${encodeURIComponent(symbol)}`, {
        state: navBackState('/auction', '集合竞价'),
      })
    },
    [navigate],
  )

  return (
    <div className="space-y-4 p-4 lg:p-6">
      {/* 头部战术指引卡 */}
      <div className="flex flex-wrap items-center justify-between gap-3 rounded-lg border border-border/50 bg-card/60 p-4">
        <div>
          <div className="flex items-center gap-2">
            <Flame className="h-5 w-5 text-orange-500" />
            <h1 className="text-[16px] font-bold text-foreground">集合竞价·盘前打板与异动雷达</h1>
            <span className="rounded bg-orange-500/10 px-2 py-0.5 text-[11px] font-semibold text-orange-500">
              9:25 决战窗口
            </span>
          </div>
          <p className="mt-1 text-[12px] text-muted-foreground">
            通达信 TQ 盘口真实竞价金额与量比穿透 · 识别真金抢筹与虚假顶板 · 捕捉弱转强妖股先锋
          </p>
        </div>

        <div className="flex items-center gap-4 text-[12px] text-muted-foreground">
          <div className="flex items-center gap-1.5">
            <Clock className="h-3.5 w-3.5 text-primary" />
            <span>9:15-9:20 试盘撤单</span>
          </div>
          <div className="flex items-center gap-1.5">
            <ShieldCheck className="h-3.5 w-3.5 text-red-500" />
            <span>9:20-9:25 不可撤单(真金)</span>
          </div>
          <div className="flex items-center gap-1.5">
            <Zap className="h-3.5 w-3.5 text-yellow-500" />
            <span>9:25-9:30 撮合定型</span>
          </div>
        </div>
      </div>

      {/* 竞价异动与爆量个股列表 */}
      <div className="rounded-lg border border-border/40 bg-card/30 p-2">
        <AuctionAnomalyTab market="CN" onOpenDetail={openAuctionDetail} />
      </div>
    </div>
  )
}
