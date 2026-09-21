import { useState } from 'react'
import { useNavigate, useSearchParams } from 'react-router-dom'
import { FileText, ShieldCheck, ArrowLeft } from 'lucide-react'
import { BrandMark } from '@/components/BrandMark'
import { Button } from '@panwatch/base-ui/components/ui/button'

/**
 * 用户协议 / 隐私政策页(合规落地, 2026-09-16)。
 * 公开路由 /terms; 注册页勾选「已阅读并同意」后可跳转阅读, 点底部按钮返回注册。
 */

type TermsTab = 'agreement' | 'privacy'

const AGREEMENT_SECTIONS: { title: string; body: string[] }[] = [
  {
    title: '一、服务说明',
    body: [
      '本平台（「数智分析 SIDA」）提供 A 股行情展示、数据分析、策略研究与信息聚合等信息服务。',
      '平台内容来自公开市场数据源及算法加工，仅供学习、研究与参考使用。',
      '平台不提供证券经纪、投资顾问、资产管理或任何形式的代客理财服务。',
    ],
  },
  {
    title: '二、用户责任',
    body: [
      '您应妥善保管账号与 API Key，因保管不善导致的损失由您自行承担。',
      '您应遵守适用法律法规及证券交易规则，不得利用本平台从事违法违规活动。',
      '您不得对平台进行逆向工程、恶意爬取、批量导出或干扰服务正常运行。',
      '您理解并同意：使用本平台产生的任何投资决策与后果，均由您自行承担。',
    ],
  },
  {
    title: '三、免责声明',
    body: [
      '本平台数据与分析结果仅供参考，不构成任何投资建议、要约或承诺。',
      '市场有风险，投资需谨慎。历史表现不代表未来收益。',
      '因数据源延迟、口径差异、系统故障、网络中断等导致的任何直接或间接损失，平台在法律允许范围内不承担责任。',
      '资金类指标存在 tick / eastmoney4 / ths 等不同口径，方向语义可能不一致，请以页面标注为准。',
    ],
  },
  {
    title: '四、知识产权',
    body: [
      '平台的软件、界面设计、算法模型与文档内容的知识产权归平台所有（第三方开源组件遵循其原许可）。',
      '未经书面许可，您不得复制、修改、分发或商业性使用平台内容。',
      '您在平台内提交的反馈与内容，平台可在改进服务的范围内合理使用。',
    ],
  },
  {
    title: '五、变更通知',
    body: [
      '平台可能不定期修订本协议。重大变更将通过站内公告、邮件或页面提示通知。',
      '变更生效后继续使用本服务，即视为您接受修订后的协议。',
      '如不同意变更内容，您应停止使用服务并注销账号。',
    ],
  },
]

const PRIVACY_SECTIONS: { title: string; body: string[] }[] = [
  {
    title: '一、我们收集哪些数据',
    body: [
      '账号信息：邮箱、用户名、加密存储的密码（不可逆哈希）。',
      '使用数据：访问日志、功能使用记录、API 调用配额与错误日志。',
      '业务数据：您创建的自选、持仓、策略参数、通知配置等。',
      '技术信息：浏览器类型、设备标识（用于安全风控与体验优化，不用于追踪个人身份）。',
    ],
  },
  {
    title: '二、我们如何使用数据',
    body: [
      '用于提供、维护与改进本服务，包括鉴权、限流、故障排查与安全防护。',
      '用于生成分析报告与个性化展示（不对外出售您的个人信息）。',
      '在法律法规要求或监管机关依法调取时，按要求配合提供必要信息。',
    ],
  },
  {
    title: '三、存储与安全',
    body: [
      '数据存储于受控服务器环境，采用访问控制、加密传输（HTTPS/TLS）等措施。',
      '密码仅以不可逆哈希形式保存；API Key 仅在创建时完整展示一次。',
      '我们按最小必要原则保留数据，并在账号注销后按策略删除或匿名化处理。',
      '尽管我们采取合理措施，仍无法保证绝对安全；发生安全事件时将依法告知。',
    ],
  },
  {
    title: '四、您的权利',
    body: [
      '查询、更正：您可在个人中心查看并修改账号与业务数据。',
      '删除与注销：您可申请删除账号及相关个人数据（法律法规另有规定的除外）。',
      '撤回同意：您可关闭非必要功能（如浏览器通知）；核心服务所需数据除外。',
      '复制导出：您有权以合理方式导出您主动提交的业务数据。',
    ],
  },
  {
    title: '五、联系方式',
    body: [
      '如对本隐私政策或个人信息处理有任何疑问、投诉或请求，可通过以下方式联系我们：',
      '在平台「设置 → 反馈」中提交；或通过项目 GitHub 仓库 Issues 联系维护者。',
      '我们将在收到请求后于合理期限内答复。',
    ],
  },
]

function SectionList({ sections }: { sections: { title: string; body: string[] }[] }) {
  return (
    <div className="space-y-6">
      {sections.map(s => (
        <section key={s.title}>
          <h3 className="mb-2 text-[13px] font-semibold text-foreground">{s.title}</h3>
          <ul className="space-y-1.5">
            {s.body.map((line, i) => (
              <li key={i} className="text-[13px] leading-relaxed text-muted-foreground">
                {line}
              </li>
            ))}
          </ul>
        </section>
      ))}
    </div>
  )
}

export default function TermsPage() {
  const navigate = useNavigate()
  const [searchParams] = useSearchParams()
  const [tab, setTab] = useState<TermsTab>(() =>
    searchParams.get('tab') === 'privacy' ? 'privacy' : 'agreement',
  )

  const handleAgree = () => {
    // 同意后回注册页（带 mode=register，避免丢失注册上下文）
    navigate('/login?mode=register')
  }

  return (
    <div className="min-h-screen bg-background">
      <div className="mx-auto w-full max-w-2xl px-4 py-8 md:py-12">
        {/* 顶栏 */}
        <div className="mb-8 flex items-center gap-3">
          <button
            type="button"
            onClick={() => navigate(-1)}
            className="flex h-8 w-8 items-center justify-center rounded-lg text-muted-foreground hover:bg-accent hover:text-foreground transition-colors"
            title="返回"
          >
            <ArrowLeft className="h-4 w-4" />
          </button>
          <div className="flex h-9 w-9 items-center justify-center rounded-xl bg-gradient-to-br from-primary to-primary/70">
            <BrandMark className="w-4 h-4 text-white" />
          </div>
          <div>
            <h1 className="text-[16px] font-bold text-foreground">服务条款</h1>
            <p className="text-[11px] text-muted-foreground">用户协议与隐私政策</p>
          </div>
        </div>

        {/* Tab 切换 */}
        <div className="mb-6 flex gap-1 rounded-lg bg-muted/50 p-1">
          <button
            type="button"
            onClick={() => setTab('agreement')}
            className={
              'flex flex-1 items-center justify-center gap-1.5 rounded-md py-2 text-[13px] transition-colors ' +
              (tab === 'agreement'
                ? 'bg-background font-medium text-foreground shadow-sm'
                : 'text-muted-foreground hover:text-foreground')
            }
          >
            <FileText className="h-3.5 w-3.5" />
            用户协议
          </button>
          <button
            type="button"
            onClick={() => setTab('privacy')}
            className={
              'flex flex-1 items-center justify-center gap-1.5 rounded-md py-2 text-[13px] transition-colors ' +
              (tab === 'privacy'
                ? 'bg-background font-medium text-foreground shadow-sm'
                : 'text-muted-foreground hover:text-foreground')
            }
          >
            <ShieldCheck className="h-3.5 w-3.5" />
            隐私政策
          </button>
        </div>

        {/* 正文卡片 */}
        <div className="card rounded-xl border border-border bg-card p-5 md:p-6 shadow-sm">
          <div className="mb-5 border-b border-border pb-4">
            <h2 className="text-[16px] font-semibold text-foreground">
              {tab === 'agreement' ? '用户协议' : '隐私政策'}
            </h2>
            <p className="mt-1 text-[11px] text-muted-foreground">
              最近更新：2026 年 9 月 16 日 · 生效日期：即日起
            </p>
          </div>
          <SectionList sections={tab === 'agreement' ? AGREEMENT_SECTIONS : PRIVACY_SECTIONS} />
        </div>

        {/* 底部同意按钮 */}
        <div className="mt-6 flex flex-col items-center gap-3">
          <Button className="w-full max-w-xs" onClick={handleAgree}>
            我已阅读并同意
          </Button>
          <p className="text-center text-[11px] leading-relaxed text-muted-foreground">
            继续注册或使用本服务，即表示您已阅读并同意《用户协议》与《隐私政策》。
            <br />
            本平台数据仅供参考，不构成投资建议。市场有风险，投资需谨慎。
          </p>
        </div>
      </div>
    </div>
  )
}
