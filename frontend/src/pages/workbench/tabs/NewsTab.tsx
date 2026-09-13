import type { ReactNode } from 'react'
import { useInsight } from '@panwatch/biz-ui/components/insight/context'
import { AnnouncementsTab } from '@panwatch/biz-ui/components/insight/AnnouncementsTab'
import { NewsTab as InsightNewsTab } from '@panwatch/biz-ui/components/insight/NewsTab'
import InsightProvider from '@/pages/workbench/InsightProvider'

/**
 * 工作台标签「消息」(工作台 v2 三合一, Task 14)。
 *
 * 落位(spec §4.3 / 计划 Task 14): **公告**(东财源, 时间窗 近7天~近180天, 另有近24/48/72小时)
 * · **新闻**(个股/相关新闻, 时间窗 近6小时~近7天)。两段各带自己的时间窗下拉。
 * 现价/名称/涨跌归带1 `HeaderBand`, 本标签一概不渲染(去重表)。
 *
 * 复用 vs 自建(brief 硬要求「REUSE them (don't re-implement)」):
 *  - 两段的**正文渲染与时间窗下拉全部复用**恢复组件 `AnnouncementsTab` / `NewsTab`(biz-ui)——
 *    它们各自持有 `Select`(值/候选项写死在组件内, 分别绑 `announcementHours` / `newsHours`)、
 *    卡片列表(`title` / `source_label · publish_time` / 外链)与空态文案;
 *  - 两个恢复组件**一字未改**(含它们内部的空态与时间窗候选项): 改它们会让本标签的区间与
 *    恢复组件的其它消费方(恢复的旧模态标签栏早已退役, 但组件属 T9 恢复面)分叉;
 *  - 本文件自建的只有: 一层 `InsightProvider` + 每段的标题/口径行/条数(恢复组件里没有),
 *    以及一段**如实的空态说明**(见下「诚实空态」)。
 *
 * 取数键 `keys={['news','announcements']}`(控制器裁定, 见 progress「Task 14 Ruling」):
 *  - `'news'` → `/news`(含 4 级兜底: 名称/代码 × filter_related 开关, 再全局扫 + 兜底
 *    `news_digest` 历史快照 —— 见 `useInsightData.loadNews`);
 *  - `'announcements'` → `/news?source=eastmoney`(同形 4 级兜底)。
 *  两个 effect 都**只按键**门控(`useInsightData:629-639`), 与内部 `tab` 无关 ⇒ 工作台标签
 *  (无旧模态标签栏, `tab` 恒 `'overview'`)只传 `keys` 即可取数, 无需任何 `setTab`(T13 复审
 *  已把 `deep`/`fundamentals`/`company` 的同类耦合解掉; 本标签用的两个键从一开始就是纯键判定)。
 *  代价: `core` 未启用 ⇒ 本标签**不取** quote/moreInfo/klineSummary/klines/portfolioSummary,
 *  故 `resolvedName`(`props.stockName || quote?.name || symbol`)恒等于 `symbol` —— 请求里
 *  不会带「用股票名检索」的第一跳(兜底链后三级仍会跑)。仅影响检索式, 不影响本标签渲染。
 *
 * 段头 vs 顶部口径条(**有意分层, 非重复**): 顶部条给一眼可读的**摘要**(来源 + 窗口区间,
 * 对应计划 Task 14 的「公告(时间窗) · 新闻(时间窗)」), 段头给**明细**(端点 + 该段下拉的全部
 * 候选项; 恢复组件的时间窗下拉正在段内, 故候选项列在段头最贴近用户视线)。
 *
 * `key={symbol}` 挂在 `InsightProvider` 上(**必要, 非风格**): `news`/`announcements` 两个
 * 数组的**重置**写在 `useInsightData:555-573` 的挂载总 effect 里, 而该 effect 第一句就是
 * `if (!isResourceEnabled(enabledKeys,'core')) return` —— 本标签**未启用 `core`** ⇒ 换标的时
 * 旧标的的文章不会被清空, 取数 effect 又要等新响应落地才覆盖 ⇒ 中间一段会**把上一只票的
 * 公告/新闻画在新标的名下**。整棵子树随 `symbol` 重挂载可消除该窗口(与 T13 的
 * `FundamentalTab` 同法; L2Tab/SuggestTab 未加, 因它们的键自带挂载重置路径)。
 *
 * 诚实空态(never fabricate):
 *  - 段内列表为空时, 复用组件给出的是「暂无公告」/「暂无相关新闻」——**不含任何编造内容**;
 *    本文件不往段内塞任何占位/示例条目。
 *  - 但恢复组件的取数**把失败静默降级为空列表**(`loadNews`/`loadAnnouncements` 的
 *    `catch { setX([]) }`, 且 hook 未暴露这两个端点的失败位), 而首拉在途时两个数组本来就是
 *    `[]` ⇒ 屏上「暂无公告」的成因有**三种**(该时间窗内确无内容 / 取数失败 / 首拉在途),
 *    UI 侧**分不出是哪一种**。故本文件在两段之上加**一行常驻说明**
 *    (`data-testid="news-empty-caveat"`), 如实陈述这一点, **不声称**空列表就是"没有内容"。
 *    根治(区分三者)需 provider 给这两个端点增失败位 + 加载位, 属跨任务 API 面 —— 见
 *    task-14-report(与 Task 11 Finding 1 同组问题)。
 *  - 段头条数只在 `> 0` 时渲染: 首拉在途时两个数组都是 `[]`, 若恒显示「共 0 条」会把
 *    「还在加载」读成「确无内容」(与 T12 头部只在其数 > 0 时给条数同法)。
 *
 * 归属 `src/pages/workbench/tabs/`: 与 T11/T12/T13 同层(标签自带 Provider, 由 Task 17 的
 * `TabPanel` 按 `?tab=` 挂载); 本文件不新增 biz-ui 依赖方向, 只消费已有导出。
 */
/** 门控键: 模块级常量 —— 每帧新建数组会换引用(Provider 现按内容签名记忆化, 此为防御性收敛)。 */
const NEWS_TAB_KEYS = ['news', 'announcements'] as const

/** 展示原子(hairline 分节, 与 L2Tab/FundamentalTab 的 Section 同形; 恢复组件里没有段头)。 */
function Section({
  id,
  title,
  hint,
  count,
  children,
}: {
  id: string
  title: string
  hint?: string
  count: number
  children: ReactNode
}) {
  return (
    <section data-testid={`news-section-${id}`} className="border-b border-border/40 pb-3">
      <div className="mb-2 flex flex-wrap items-baseline gap-x-2 gap-y-0.5 text-[11px]">
        <span className="font-medium text-foreground">{title}</span>
        {hint ? (
          <span className="text-muted-foreground" title={hint}>
            {hint}
          </span>
        ) : null}
        {/* 只在有数据时给条数: 首拉在途两个数组都是 [], 恒显「共 0 条」会被读成"确无内容" */}
        {count > 0 ? <span className="font-mono text-[10px] text-muted-foreground">共 {count} 条</span> : null}
      </div>
      {children}
    </section>
  )
}

/** 标签正文(在 Provider 内消费 useInsight; 见文件头注)。 */
function NewsTabBody() {
  const { announcements, news } = useInsight()
  return (
    <div className="mt-1 space-y-3 text-[12px]" data-testid="news-tab">
      {/* 条: 口径说明 + 空态说明(无现价/名称/涨跌 —— 带1 拥有) */}
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1 border-b border-border/40 pb-2 text-[11px] text-muted-foreground">
        <span className="text-foreground">消息</span>
        <span className="text-border/60">|</span>
        <span>公告(东财, 时间窗 近7天~近180天) · 新闻(时间窗 近6小时~近7天)</span>
        {/* 如实披露: 两个端点失败被静默降级为空列表(hook 无失败位), 且首拉在途两数组也是 [] ⇒
            空态不可被读作"确无内容"(三种成因在 UI 侧不可区分) */}
        <span className="ml-auto text-[10px]" data-testid="news-empty-caveat">
          列表为空时「该时间窗内确无内容 / 取数失败 / 首拉在途」在此不可区分
        </span>
      </div>

      {/* 公告(其时间窗下拉与列表由恢复组件 AnnouncementsTab 自带) */}
      <Section
        id="announcements"
        title="公告"
        hint="/news?source=eastmoney(东财) · 时间窗 近7天/近14天/近30天/近90天/近180天 与 近24/48/72小时"
        count={announcements.length}
      >
        <AnnouncementsTab />
      </Section>

      {/* 新闻(其时间窗下拉与列表由恢复组件 NewsTab 自带) */}
      <Section
        id="news"
        title="新闻"
        hint="/news(个股/相关新闻, 名称/代码 双跳 + 全局扫 + 历史快照兜底) · 时间窗 近6/12/24/48小时 与 近7天"
        count={news.length}
      >
        <InsightNewsTab />
      </Section>
    </div>
  )
}

/**
 * 标签入口。`keys={NEWS_TAB_KEYS}`(模块级常量): 只启用 `/news` 与 `/news?source=eastmoney`
 * 两个端点 —— quote/moreInfo/darkFlowTq/klineSummary/klines/portfolioSummary/watchlist/
 * suggestions/reports/deep/fundamentals/company 一个都不发(惰性由 `TabPanel` 只渲染激活标签
 * 保证)。`key={symbol}`: 换标的整体重挂载(理由见文件头注 —— 本标签未启用 `core`, 挂载总
 * effect 的空值重置路径不会跑)。`hasPosition` 本标签**不消费**(无按持仓分支的内容), 仅与
 * 兄弟标签同形透传给 Provider(Task 17 接线时可统一传参)。
 */
export default function NewsTab({
  symbol,
  market,
  hasPosition,
}: {
  symbol: string
  market: string
  hasPosition?: boolean
}) {
  return (
    <InsightProvider key={symbol} symbol={symbol} market={market} hasPosition={hasPosition} keys={NEWS_TAB_KEYS}>
      <NewsTabBody />
    </InsightProvider>
  )
}
