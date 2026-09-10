/**
 * 板块热力图页面 (P1-1, 2026-09-10, 借鉴 OpenTerminal treemap 热力图):
 * 行业/概念板块 treemap, 面积=成交额或等权, 颜色=涨跌幅(A股红涨绿跌, ±3% 夹紧),
 * 点击色块下钻成分股(复用 /boards/:blockCode 详情页)。
 */
import { useNavigate } from 'react-router-dom'
import BoardHeatmap from '@panwatch/biz-ui/components/dashboard/BoardHeatmap'

export default function HeatmapPage() {
  const navigate = useNavigate()
  return (
    <div className="sida-page-enter space-y-4">
      <div>
        <h1 className="text-[16px] font-semibold">板块热力图</h1>
        <p className="mt-0.5 text-[12px] text-muted-foreground">
          面积=量能(成交额口径，缺失板块以最小面积保底)或等权；颜色=当日涨跌幅(±3% 夹紧，平盘/无数据为灰)。点击色块查看成分股。
        </p>
      </div>
      <div className="card p-3">
        <BoardHeatmap
          onOpenBoard={(blockCode) => navigate(`/boards/${encodeURIComponent(blockCode)}`)}
        />
      </div>
    </div>
  )
}
