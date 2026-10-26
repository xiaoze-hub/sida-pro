import { useCallback } from 'react'
import { useNavigate } from 'react-router-dom'
import PageHeader from '@/components/PageHeader'
import { StatusChip } from '@panwatch/base-ui/components/ui/status-chip'
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
    <div className="page-container space-y-4">
      <PageHeader title="集合竞价" description="对照竞价金额、量比与撤单变化，查看异常结构与数据证据。"
        metadata={<><StatusChip>09:15–09:20 可撤单</StatusChip><StatusChip tone="info">09:20–09:25 不可撤单</StatusChip><StatusChip>09:25–09:30 撮合结果</StatusChip></>}
      />

      {/* 竞价异动与爆量个股列表 */}
      <div className="analysis-section">
        <AuctionAnomalyTab market="CN" onOpenDetail={openAuctionDetail} />
      </div>
    </div>
  )
}
