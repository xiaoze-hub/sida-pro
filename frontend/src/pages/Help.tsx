import { MessageSquare, LineChart, Sparkles, Wallet, FileText, Keyboard, Download, HelpCircle } from 'lucide-react'

/**
 * 帮助中心 — 功能说明 + 快捷键。
 * 风格与 Settings 一致(卡片布局), 不使用 emoji/渐变。
 */
const FEATURES = [
  {
    icon: MessageSquare,
    title: '对话助手',
    desc: '全局 AI 助手，基于持仓/自选/行情上下文回答股票问题。',
    tips: [
      '右下角悬浮按钮随时唤起，支持多轮对话与历史记录',
      '可直接问“今天该看什么”“某只票怎么走”，回答会引用行情与新闻',
      '对话内容可一键生成图文分享卡片',
    ],
  },
  {
    icon: LineChart,
    title: '预测',
    desc: '多模型(时序 + 机器学习)对个股未来走势做概率预测。',
    tips: [
      'Kronos / TimesFM / XGBoost 多模型加权，附分位数区间',
      '预测会叠加情绪面与新闻事件修正，非单一模型输出',
      '历史预测自动回测，命中率用于动态调整模型权重',
    ],
  },
  {
    icon: Sparkles,
    title: '机会',
    desc: '多源候选池 + 共振查询:一句话让问小达+问财双引擎共识选股。',
    tips: [
      '共振查询:输入题材/条件 → 双引擎并发合并,🔥×N=被 N 个引擎同时命中,双引擎共识最可信;可选策略库规则对结果精筛(只扫结果,不重复调引擎)',
      '候选必须带入场区间 / 目标价 / 止损,无计划不入选;🔥 标记 = 被多个独立来源命中的候选',
      '统一筛选:点「筛选」按市场/来源/持仓/评分/风险过滤,弹层内改动需点「应用」才生效(按钮数字=已生效条件数)',
      '「信号策略」过滤的是信号来源标签,与选股工具里的策略库(可执行规则)是两个口径,别混淆',
    ],
  },
  {
    icon: Wallet,
    title: '影子账户',
    desc: '用模拟资金跟踪策略表现，不占用真实资金。',
    tips: [
      '适合先验证策略，再决定是否实盘',
      '支持创建多组模拟账户对比不同策略',
      '持仓与盈亏独立统计，不影响真实持仓页',
    ],
  },
  {
    icon: FileText,
    title: '报告',
    desc: '盘前扫描 / 盘后复盘的自动化分析报告。',
    tips: [
      '报告由 AI 聚合市场情绪、涨停结构与资金流向生成',
      '可推送至企业微信等通知渠道',
      '历史报告可在报告页回看与导出',
    ],
  },
]

const HOTKEYS = [
  { keys: 'Ctrl / Cmd + K', desc: '聚焦搜索框，无搜索框时打开日志面板' },
  { keys: 'Ctrl / Cmd + ,', desc: '打开设置页' },
  { keys: 'g 然后 d', desc: '回到首页(1.5 秒内依次按下)' },
  { keys: 'g 然后 p', desc: '跳转持仓页' },
  { keys: '?', desc: '打开键盘快捷键帮助' },
]

export default function HelpPage() {
  return (
    <div className="page-container pb-10">
      {/* Hero */}
      <div className="card p-5 md:p-7">
        <div className="flex items-start gap-3">
          <div className="flex h-9 w-9 shrink-0 items-center justify-center rounded-lg bg-accent/40 text-primary ring-1 ring-border/40">
            <HelpCircle className="h-4.5 w-4.5" />
          </div>
          <div>
            <h1 className="text-[20px] md:text-[22px] font-bold text-foreground tracking-tight">帮助中心</h1>
            <p className="text-[12px] text-muted-foreground mt-1">
              PanWatch 功能速览与操作提示，数据导出与快捷键一览
            </p>
          </div>
        </div>
      </div>

      {/* 功能说明卡片 */}
      <div className="mt-6 grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
        {FEATURES.map(f => (
          <section key={f.title} className="card p-4 md:p-5">
            <div className="flex items-center gap-2.5 mb-2.5">
              <div className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg bg-accent/40 text-primary ring-1 ring-border/40">
                <f.icon className="h-4 w-4" />
              </div>
              <h3 className="text-[13px] font-semibold text-foreground">{f.title}</h3>
            </div>
            <p className="text-[12px] text-foreground/80 leading-relaxed">{f.desc}</p>
            <ul className="mt-3 space-y-1.5">
              {f.tips.map((tip, i) => (
                <li key={i} className="flex items-start gap-1.5 text-[11px] text-muted-foreground leading-relaxed">
                  <span className="mt-[7px] h-1 w-1 shrink-0 rounded-full bg-border" />
                  <span>{tip}</span>
                </li>
              ))}
            </ul>
          </section>
        ))}

        {/* 数据导出 */}
        <section className="card p-4 md:p-5">
          <div className="flex items-center gap-2.5 mb-2.5">
            <div className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg bg-accent/40 text-primary ring-1 ring-border/40">
              <Download className="h-4 w-4" />
            </div>
            <h3 className="text-[13px] font-semibold text-foreground">数据导出</h3>
          </div>
          <p className="text-[12px] text-foreground/80 leading-relaxed">持仓、预测记录与机会候选均可一键导出 CSV。</p>
          <ul className="mt-3 space-y-1.5">
            <li className="flex items-start gap-1.5 text-[11px] text-muted-foreground leading-relaxed">
              <span className="mt-[7px] h-1 w-1 shrink-0 rounded-full bg-border" />
              <span>持仓页 / 预测页 / 机会页顶部均有「导出」按钮</span>
            </li>
            <li className="flex items-start gap-1.5 text-[11px] text-muted-foreground leading-relaxed">
              <span className="mt-[7px] h-1 w-1 shrink-0 rounded-full bg-border" />
              <span>CSV 为 UTF-8 带 BOM，Excel 直接打开中文不乱码</span>
            </li>
            <li className="flex items-start gap-1.5 text-[11px] text-muted-foreground leading-relaxed">
              <span className="mt-[7px] h-1 w-1 shrink-0 rounded-full bg-border" />
              <span>无数据时导出空表头文件，不会报错</span>
            </li>
          </ul>
        </section>
      </div>

      {/* 快捷键 */}
      <section className="card p-4 md:p-5 mt-4">
        <div className="flex items-center gap-2.5 mb-3">
          <div className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg bg-accent/40 text-primary ring-1 ring-border/40">
            <Keyboard className="h-4 w-4" />
          </div>
          <h3 className="text-[13px] font-semibold text-foreground">快捷键</h3>
          <span className="text-[10px] text-muted-foreground ml-auto hidden sm:inline">桌面端(≥768px)生效，输入框内普通键不触发</span>
        </div>
        <div className="grid grid-cols-1 sm:grid-cols-2 gap-x-6 gap-y-2.5">
          {HOTKEYS.map((h, i) => (
            <div key={i} className="flex items-center justify-between gap-3 rounded-lg border border-border/50 bg-accent/20 px-3 py-2">
              <kbd className="font-mono text-[11px] text-primary whitespace-nowrap">{h.keys}</kbd>
              <span className="text-[11px] text-muted-foreground text-right">{h.desc}</span>
            </div>
          ))}
        </div>
      </section>
    </div>
  )
}
