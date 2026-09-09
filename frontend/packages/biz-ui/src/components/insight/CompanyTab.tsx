import { useInsight } from './context'

export function CompanyTab() {
  const {
    companyInfo,
    companyLoading,
  } = useInsight()
  return (
    <div className="space-y-3">
      {companyLoading ? (
        <div className="card p-6 text-[12px] text-muted-foreground text-center">加载公司信息...</div>
      ) : !companyInfo || (!companyInfo.name && companyInfo.note) ? (
        <div className="card p-6 text-[12px] text-muted-foreground text-center">
          {companyInfo?.note || '暂无公司信息'}
        </div>
      ) : (
        <div className="space-y-3">
          {/* 主营 */}
          {companyInfo.bscope && (
            <div className="card p-4">
              <div className="text-[11px] text-muted-foreground mb-1">主营业务</div>
              <div className="text-[13px] text-foreground font-medium">{companyInfo.bscope}</div>
            </div>
          )}
          {/* 公司简介 */}
          {companyInfo.desc && (
            <div className="card p-4">
              <div className="text-[11px] text-muted-foreground mb-1">公司简介</div>
              <div className="text-[12px] text-foreground/90 leading-relaxed">{companyInfo.desc}</div>
            </div>
          )}
          {/* 基本信息网格 */}
          <div className="card p-4">
            <div className="text-[11px] text-muted-foreground mb-2">基本信息</div>
            <div className="grid grid-cols-2 gap-x-4 gap-y-2 text-[12px]">
              {companyInfo.name && (
                <>
                  <div className="text-muted-foreground">公司全称</div>
                  <div className="text-foreground text-right break-words">{companyInfo.name}</div>
                </>
              )}
              {companyInfo.ename && (
                <>
                  <div className="text-muted-foreground">英文名</div>
                  <div className="text-foreground text-right break-words">{companyInfo.ename}</div>
                </>
              )}
              {companyInfo.market_board && (
                <>
                  <div className="text-muted-foreground">交易所</div>
                  <div className="text-foreground text-right">{companyInfo.market_board}</div>
                </>
              )}
              {companyInfo.list_date && (
                <>
                  <div className="text-muted-foreground">上市日期</div>
                  <div className="text-foreground text-right">{companyInfo.list_date}</div>
                </>
              )}
              {companyInfo.reg_capital && (
                <>
                  <div className="text-muted-foreground">注册资本</div>
                  <div className="text-foreground text-right">{companyInfo.reg_capital}</div>
                </>
              )}
              {companyInfo.list_status && (
                <>
                  <div className="text-muted-foreground">企业性质</div>
                  <div className="text-foreground text-right">{companyInfo.list_status}</div>
                </>
              )}
              {companyInfo.issuer && (
                <>
                  <div className="text-muted-foreground">主承销商</div>
                  <div className="text-foreground text-right">{companyInfo.issuer}</div>
                </>
              )}
              {companyInfo.secretary && (
                <>
                  <div className="text-muted-foreground">董秘</div>
                  <div className="text-foreground text-right">{companyInfo.secretary}</div>
                </>
              )}
              {companyInfo.area && (
                <>
                  <div className="text-muted-foreground">注册地址</div>
                  <div className="text-foreground text-right break-words">{companyInfo.area}</div>
                </>
              )}
              {companyInfo.website && (
                <>
                  <div className="text-muted-foreground">官网</div>
                  <a href={companyInfo.website} target="_blank" rel="noreferrer" className="text-right text-primary hover:underline break-words">{companyInfo.website}</a>
                </>
              )}
            </div>
          </div>
          {/* 概念板块 */}
          {companyInfo.concepts && (
            <div className="card p-4">
              <div className="text-[11px] text-muted-foreground mb-2">概念板块</div>
              <div className="flex flex-wrap gap-1.5">
                {companyInfo.concepts.split(',').filter(Boolean).map((c) => (
                  <span key={c} className="rounded-full bg-accent/50 px-2 py-0.5 text-[11px] text-foreground/80">{c.trim()}</span>
                ))}
              </div>
            </div>
          )}
        </div>
      )}
    </div>
  )
}
