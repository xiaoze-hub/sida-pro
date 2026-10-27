import PageHeader from '@/components/PageHeader'
/**
 * 板块热力图页面 (P1-1, 2026-09-10, 借鉴 OpenTerminal treemap 热力图):
 * 行业/概念板块 treemap, 面积=成交额或等权, 颜色=涨跌幅(A股红涨绿跌, ±3% 夹紧),
 * 点击色块下钻成分股(复用 /boards/:blockCode 详情页)。
 */
import { useNavigate } from 'react-router-dom'
import BoardHeatmap from '@sida/biz-ui/components/dashboard/BoardHeatmap'
import { navBackState } from '@/lib/nav-back'

export default function HeatmapPage() {
  const navigate = useNavigate()
  return (
    <div className="sida-page-enter space-y-4">
      <PageHeader title="板块热力" description="面积表示量能或等权，颜色表示涨跌幅（±3%）；无数据为灰色。点击板块查看成分股。" />
      <div className="analysis-section">
        <BoardHeatmap
          onOpenBoard={(blockCode) => navigate(`/boards/${encodeURIComponent(blockCode)}`, { state: navBackState('/heatmap', '板块热力图') })}
        />
      </div>
    </div>
  )
}
