import { Dialog, DialogContent } from '@panwatch/base-ui/components/ui/dialog'
import type { StockInsightModalProps } from './insight/types'
import { InsightContext } from './insight/context'
import { useInsightData } from './insight/useInsightData'
import { useInsightDerived } from './insight/useInsightDerived'
import { useInsightActions } from './insight/useInsightActions'
import { InsightHeaderBar } from './insight/InsightHeaderBar'
import { OverviewTab } from './insight/OverviewTab'
import { KlineTab } from './insight/KlineTab'
import { ReportsTab } from './insight/ReportsTab'
import { DeepTab } from './insight/DeepTab'
import { SuggestionsTab } from './insight/SuggestionsTab'
import { NewsTab } from './insight/NewsTab'
import { AnnouncementsTab } from './insight/AnnouncementsTab'
import { CompanyTab } from './insight/CompanyTab'
import { FundamentalsTab } from './insight/FundamentalsTab'

export type { StockInsightModalProps } from './insight/types'

export default function StockInsightModal(props: StockInsightModalProps) {
  const data = useInsightData(props)
  const derived = useInsightDerived(props, data)
  const actions = useInsightActions(props, data, derived)
  const ctx = { props, ...data, ...derived, ...actions }

  return (
    <InsightContext.Provider value={ctx}>
      <Dialog open={props.open} onOpenChange={props.onOpenChange}>
        <DialogContent className="w-[92vw] max-w-6xl p-5 md:p-6 overflow-x-hidden">
          <InsightHeaderBar />
          <div className="max-h-[68vh] overflow-y-auto overflow-x-hidden pr-1 scrollbar">
            {data.tab === 'overview' && <OverviewTab />}
            {data.tab === 'kline' && <KlineTab />}
            {data.tab === 'reports' && <ReportsTab />}
            {data.tab === 'deep' && <DeepTab />}
            {data.tab === 'suggestions' && <SuggestionsTab />}
            {data.tab === 'news' && <NewsTab />}
            {data.tab === 'announcements' && <AnnouncementsTab />}
            {data.tab === 'company' && <CompanyTab />}
            {data.tab === 'fundamentals' && <FundamentalsTab />}
          </div>
        </DialogContent>
      </Dialog>
    </InsightContext.Provider>
  )
}
