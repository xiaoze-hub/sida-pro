import { useCallback, useEffect, useMemo, useState } from 'react'
import { Scale } from 'lucide-react'
import { factorsApi, getToken, type FactorCalibrationResp, type FactorWeight } from '@sida/api'
import { Switch } from '@sida/base-ui/components/ui/switch'
import { useMyPermissions } from '@/hooks/useMyPermissions'

const FACTOR_LABELS: Record<string, string> = {
  alpha_score: '选股α',
  catalyst_score: '催化',
  quality_score: '计划质量',
  risk_penalty: '风险',
  crowd_penalty: '拥挤度',
}

const MARKET_LABELS: Record<string, string> = {
  CN: 'A股',
  HK: '港股',
  US: '美股',
}

const MARKET_ORDER: Record<string, number> = { CN: 0, HK: 1, US: 2 }
const FACTOR_ORDER = ['alpha_score', 'catalyst_score', 'quality_score', 'risk_penalty', 'crowd_penalty']

const STATUS_LABELS: Record<string, string> = {
  proposed: '有建议',
  unchanged: '无需调整',
  skipped_no_data: '无已走完持有期的样本',
  skipped_low_sample: '样本不足',
  skipped_ic_unavailable: '期数不足, 算不出 IC',
  skipped_oos_insufficient: '样本外不成立',
  skipped_oos_sign_flip: '样本外符号翻转',
  skipped_pinned: '已锁定(不出建议)',
  skipped_disabled: '已关闭标定建议',
}

const factorLabel = (code: string) => FACTOR_LABELS[code] || code
const marketLabel = (market: string) => MARKET_LABELS[market] || market
const statusLabel = (status: string) => STATUS_LABELS[status] || status

const formatMetric = (value: number | null | undefined, digits = 3) => {
  if (value == null || Number.isNaN(value)) return '--'
  return Number(value).toFixed(digits)
}

/** 观测时间只显示到分钟; 解析失败时原样带出, 不装作「刚刚」。 */
const formatObservedAt = (value: string | null) => {
  if (!value) return '从未记录'
  const d = new Date(value)
  if (Number.isNaN(d.getTime())) return value
  return d.toLocaleString('zh-CN', { hour12: false })
}

const rowKey = (item: FactorWeight) => `${item.market}:${item.factor_code}`

export default function FactorWeightsPanel() {
  const [items, setItems] = useState<FactorWeight[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [saving, setSaving] = useState<string | null>(null)
  const [calibMarket, setCalibMarket] = useState('CN')
  const [calib, setCalib] = useState<FactorCalibrationResp | null>(null)
  const [calibLoading, setCalibLoading] = useState(false)
  const [calibError, setCalibError] = useState('')

  const { permissions } = useMyPermissions(getToken())
  // 写权限由服务端判(manage_settings); 这里只决定按钮是否可点, 不是安全边界。
  const canManage = permissions?.has('manage_settings') ?? false

  const load = useCallback(async () => {
    setLoading(true)
    setError('')
    try {
      const res = await factorsApi.list()
      setItems(res.items || [])
    } catch (e) {
      setError(e instanceof Error ? e.message : '加载失败')
      setItems([])
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    load()
  }, [load])

  const sortedItems = useMemo(() => {
    return [...items].sort((a, b) => {
      const marketDelta = (MARKET_ORDER[a.market] ?? 99) - (MARKET_ORDER[b.market] ?? 99)
      if (marketDelta !== 0) return marketDelta
      const aFactor = FACTOR_ORDER.indexOf(a.factor_code)
      const bFactor = FACTOR_ORDER.indexOf(b.factor_code)
      return (aFactor < 0 ? 99 : aFactor) - (bFactor < 0 ? 99 : bFactor)
    })
  }, [items])

  const updateFlag = useCallback(
    async (item: FactorWeight, patch: { is_pinned?: boolean; auto_calibrate?: boolean }) => {
      const key = rowKey(item)
      setSaving(key)
      try {
        await factorsApi.update(item.factor_code, item.market, patch)
        await load()
      } catch (e) {
        setError(e instanceof Error ? e.message : '更新失败')
      } finally {
        setSaving(null)
      }
    },
    [load],
  )

  const runCalibration = useCallback(async () => {
    setCalibLoading(true)
    setCalibError('')
    try {
      const res = await factorsApi.calibrate(calibMarket)
      setCalib(res)
      if (res.ok === false) setCalibError(res.error || '标定计算失败')
    } catch (e) {
      setCalib(null)
      setCalibError(e instanceof Error ? e.message : '标定计算失败')
    } finally {
      setCalibLoading(false)
    }
  }, [calibMarket])

  return (
    <section id="sec-factors" className="card p-4 md:p-6 lg:col-span-12">
      <div className="flex items-start justify-between mb-4 gap-3">
        <div>
          <h3 className="text-[12px] md:text-[14px] font-semibold text-foreground flex items-center gap-1.5">
            <Scale className="w-3.5 h-3.5 text-muted-foreground" />
            因子权重(手动 + 只读建议)
          </h3>
          <p className="text-[12px] text-muted-foreground mt-1">
            权重只能由有权限的人手动覆盖; 系统不会自动调权。「最近IC」是历史观测(附观测时间, 过期会标注),
            实时口径请点「计算建议」(只算建议, 不落库)。
          </p>
          {!canManage && (
            <p className="text-[12px] text-muted-foreground mt-1">
              只读: 调整权重/开关需要 manage_settings(owner 授予), 否则服务端会拒绝。
            </p>
          )}
        </div>
        <div className="flex items-center gap-1.5">
          {(['CN', 'HK', 'US'] as const).map((m) => (
            <button
              key={m}
              type="button"
              onClick={() => setCalibMarket(m)}
              className={`px-2 py-1 text-[12px] rounded border ${
                calibMarket === m
                  ? 'border-primary text-foreground'
                  : 'border-border text-muted-foreground'
              }`}
            >
              {marketLabel(m)}
            </button>
          ))}
          <button
            type="button"
            onClick={runCalibration}
            disabled={calibLoading}
            className="px-2.5 py-1 text-[12px] rounded border border-border text-foreground disabled:opacity-50"
          >
            {calibLoading ? '计算中…' : '计算建议'}
          </button>
        </div>
      </div>

      {error && (
        <div className="mb-3 text-[12px] text-amber-500">{error}</div>
      )}

      {loading ? (
        <div className="flex items-center justify-center py-10">
          <span className="w-5 h-5 border-2 border-primary/30 border-t-primary rounded-full animate-spin" />
        </div>
      ) : sortedItems.length === 0 ? (
        <div className="text-[12px] text-muted-foreground text-center py-6">暂无因子权重数据</div>
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full text-[12px]">
            <thead>
              <tr className="text-left text-[12px] text-muted-foreground border-b border-border">
                <th className="py-2 pr-3 font-medium">因子</th>
                <th className="py-2 pr-3 font-medium">市场</th>
                <th className="py-2 pr-3 font-medium text-right">权重</th>
                <th className="py-2 pr-3 font-medium text-right">最近IC(历史)</th>
                <th className="py-2 pr-3 font-medium text-right">最近IR</th>
                <th className="py-2 pr-3 font-medium text-right">样本</th>
                <th className="py-2 pr-3 font-medium">观测时间</th>
                <th className="py-2 pr-3 font-medium text-center">锁定</th>
                <th
                  className="py-2 font-medium text-center"
                  title="仅决定只读标定是否为该因子出建议; 任何开关都不会自动改权重"
                >
                  标定建议
                </th>
              </tr>
            </thead>
            <tbody>
              {sortedItems.map((item) => {
                const key = rowKey(item)
                const busy = saving === key
                return (
                  <tr key={key} className="border-b border-border last:border-0">
                    <td className="py-2 pr-3 text-foreground">{factorLabel(item.factor_code)}</td>
                    <td className="py-2 pr-3 text-muted-foreground">{marketLabel(item.market)}</td>
                    <td className="py-2 pr-3 text-right font-mono text-foreground">{formatMetric(item.weight, 2)}</td>
                    <td className="py-2 pr-3 text-right font-mono text-muted-foreground">{formatMetric(item.last_ic)}</td>
                    <td className="py-2 pr-3 text-right font-mono text-muted-foreground">{formatMetric(item.last_ir)}</td>
                    <td className="py-2 pr-3 text-right font-mono text-muted-foreground">{item.last_sample_size ?? '--'}</td>
                    <td className="py-2 pr-3 text-muted-foreground whitespace-nowrap" title={item.observation_note}>
                      {formatObservedAt(item.last_observation_at)}
                      {item.last_observation_stale && (
                        <span className="ml-1 text-amber-500">过期</span>
                      )}
                    </td>
                    <td className="py-2 pr-3">
                      <div className="flex justify-center">
                        <Switch
                          checked={item.is_pinned}
                          disabled={busy || !canManage}
                          onCheckedChange={(v) => updateFlag(item, { is_pinned: v })}
                        />
                      </div>
                    </td>
                    <td className="py-2">
                      <div className="flex justify-center">
                        <Switch
                          checked={item.auto_calibrate}
                          disabled={busy || !canManage}
                          title="关闭后只读标定会跳过该因子; 不会自动改权重"
                          onCheckedChange={(v) => updateFlag(item, { auto_calibrate: v })}
                        />
                      </div>
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        </div>
      )}

      {calibError && (
        <div className="mt-3 text-[12px] text-amber-500">标定失败: {calibError}</div>
      )}

      {calib && (
        <div className="mt-4 border-t border-border pt-3">
          <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-[12px] text-muted-foreground mb-2">
            <span className="text-foreground font-medium">
              只读标定建议 · {marketLabel(calib.market)}
            </span>
            <span>计算时间: {formatObservedAt(calib.computed_at)}{calib.cached ? '(缓存)' : ''}</span>
            <span>权重行: {calib.rows_present}{calib.no_rows ? '(尚无, 未自动建行)' : ''}</span>
            <span>有建议: {calib.proposed}</span>
            <span>已生效: 0(建议不落库)</span>
            {calib.ok === false && <span className="text-amber-500">计算失败: {calib.error}</span>}
          </div>
          <div className="overflow-x-auto">
            <table className="w-full text-[12px]">
              <thead>
                <tr className="text-left text-[12px] text-muted-foreground border-b border-border">
                  <th className="py-1.5 pr-3 font-medium">因子</th>
                  <th className="py-1.5 pr-3 font-medium text-right">当前</th>
                  <th className="py-1.5 pr-3 font-medium text-right">建议</th>
                  <th className="py-1.5 pr-3 font-medium text-right">IC</th>
                  <th className="py-1.5 pr-3 font-medium text-right">样本</th>
                  <th className="py-1.5 font-medium">状态</th>
                </tr>
              </thead>
              <tbody>
                {(calib.proposals || []).map((p) => (
                  <tr key={p.factor_code} className="border-b border-border last:border-0">
                    <td className="py-1.5 pr-3 text-foreground">{factorLabel(p.factor_code)}</td>
                    <td className="py-1.5 pr-3 text-right font-mono text-muted-foreground">{formatMetric(p.current_weight, 2)}</td>
                    <td className="py-1.5 pr-3 text-right font-mono text-foreground">
                      {p.proposed_weight == null ? '--' : formatMetric(p.proposed_weight, 2)}
                    </td>
                    <td className="py-1.5 pr-3 text-right font-mono text-muted-foreground">{formatMetric(p.ic)}</td>
                    <td className="py-1.5 pr-3 text-right font-mono text-muted-foreground">{p.sample_size}</td>
                    <td className="py-1.5 text-muted-foreground">{statusLabel(p.status)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}
    </section>
  )
}
