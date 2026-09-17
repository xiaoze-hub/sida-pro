import { useCallback, useEffect, useMemo, useState } from 'react'
import { Lock, RefreshCw, Save, SlidersHorizontal, Loader2, CheckCircle2 } from 'lucide-react'
import { Button } from '@panwatch/base-ui/components/ui/button'
import { freeTierApi, type FreeTierPatch, type FreeTierResponse } from '@panwatch/api'

/**
 * 「免费档」面板(2026-09-18) —— owner 专属, 运行时调整**免费级别**。
 *
 * 为什么要有它: 原先把"member 能试用什么/每天几次/哪个 skill 免费"写死在代码里, 改一次要发版。
 * 现在改完 **30s 内**全节点热生效(后端 30s TTL 缓存 + 写后立即失效)。
 *
 * 诚实口径: 只渲染后端给的真实配置(非 owner 拿 403 → 整块不显示, 不画"假开关");
 * 「默认口径」一并展示, 方便对照"我改成了什么 / 原本是什么"。
 */
export function FreeTierSection() {
  const [data, setData] = useState<FreeTierResponse | null>(null)
  const [hidden, setHidden] = useState(false)
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)
  const [msg, setMsg] = useState('')
  const [err, setErr] = useState('')

  // 草稿态(保存后回填服务端值)
  const [trial, setTrial] = useState<Record<string, boolean>>({})
  const [dailyLimit, setDailyLimit] = useState(3)
  const [watchMax, setWatchMax] = useState(10)
  const [alertMax, setAlertMax] = useState(3)
  const [skillTier, setSkillTier] = useState<Record<string, string>>({})

  // useCallback: 下面 effect 依赖它 → 每次渲染换新引用会让 effect 反复重跑
  const apply = useCallback((d: FreeTierResponse) => {
    setData(d)
    const t: Record<string, boolean> = {}
    for (const f of d.catalog.features) t[f.perm] = f.trial
    setTrial(t)
    setDailyLimit(d.config.trial_daily_limit)
    setWatchMax(d.config.member_watchlist_max)
    setAlertMax(d.config.member_alert_max)
    setSkillTier({ ...d.config.skill_tier_overrides })
  }, [])

  const load = async () => {
    setErr('')
    try {
      const d = await freeTierApi.get()
      apply(d)
      setHidden(false)
    } catch (e: any) {
      // 非 owner(403) → 静默隐藏; 其它错误要给用户看得见的失败, 不假装成功
      const status = e?.status ?? e?.statusCode
      if (status === 403) setHidden(true)
      else setErr(e?.message || '免费档配置读取失败')
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    void load()
    // 只在挂载时跑一次: load 里只用到稳定的 setter 与 api, 故意不列进依赖
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const patch = useMemo<FreeTierPatch>(() => {
    const selected: Record<string, string> = {}
    for (const f of data?.catalog.features || []) {
      if (trial[f.perm]) selected[f.perm] = f.label
    }
    return {
      trial_features: selected,
      trial_daily_limit: dailyLimit,
      member_watchlist_max: watchMax,
      member_alert_max: alertMax,
      skill_tier_overrides: skillTier,
    }
  }, [data, trial, dailyLimit, watchMax, alertMax, skillTier])

  const save = async () => {
    setSaving(true)
    setMsg('')
    setErr('')
    try {
      const r = await freeTierApi.update(patch)
      apply({ ...(data as FreeTierResponse), config: r.config, catalog: r.catalog })
      setMsg('已保存 · 30s 内全节点生效(无需重启)')
    } catch (e: any) {
      setErr(e?.message || '保存失败')
    } finally {
      setSaving(false)
    }
  }

  if (hidden) return null
  if (loading) {
    return (
      <div className="lg:col-span-12 text-[12px] text-muted-foreground flex items-center gap-2">
        <Loader2 className="w-3.5 h-3.5 animate-spin" /> 正在读取免费档配置...
      </div>
    )
  }
  if (!data) {
    return err ? (
      <div className="lg:col-span-12 text-[12px] text-red-500">免费档：{err}</div>
    ) : null
  }

  const d = data.defaults
  const proSkills = data.catalog.skills

  return (
    <div className="lg:col-span-12 border border-border/50 rounded-lg p-4 space-y-4">
      <div className="flex items-center justify-between">
        <h3 className="text-[14px] font-semibold text-foreground flex items-center gap-2">
          <SlidersHorizontal className="w-4 h-4 text-primary" /> 免费档（免费级别）
        </h3>
        <div className="flex items-center gap-2">
          <Button size="sm" variant="outline" className="h-7 text-[11px]" onClick={() => void load()}>
            <RefreshCw className="w-3 h-3 mr-1" /> 重新读取
          </Button>
          <Button size="sm" className="h-7 text-[11px]" disabled={saving} onClick={() => void save()}>
            {saving ? <Loader2 className="w-3 h-3 mr-1 animate-spin" /> : <Save className="w-3 h-3 mr-1" />}
            保存
          </Button>
        </div>
      </div>

      <p className="text-[11px] text-muted-foreground">
        决定 <b>member(免费用户)</b> 能用什么、每天几次。数智决策三指标与集合竞价池默认是
        <b> pro 专属</b>（不在免费层级）—— 这里勾上即临时放开试用。
      </p>

      {(msg || err) && (
        <div className={`text-[11px] ${err ? 'text-red-500' : 'text-emerald-500'} flex items-center gap-1`}>
          {!err && <CheckCircle2 className="w-3.5 h-3.5" />}
          {err || msg}
        </div>
      )}

      {/* ① pro 专属功能 → 是否给免费试用 */}
      <div>
        <div className="text-[12px] font-medium text-foreground mb-2">可试用功能</div>
        <div className="flex flex-wrap gap-3">
          {data.catalog.features.map((f) => (
            <label key={f.perm} className="flex items-center gap-1.5 text-[12px] text-foreground">
              <input
                type="checkbox"
                aria-label={`试用-${f.label}`}
                checked={!!trial[f.perm]}
                onChange={(e) => setTrial((s) => ({ ...s, [f.perm]: e.target.checked }))}
              />
              {f.label}
              {d.trial_features[f.perm] ? (
                <span className="text-[10px] text-muted-foreground">（默认就给）</span>
              ) : (
                <span className="text-[10px] text-amber-500">（默认 pro 专属）</span>
              )}
            </label>
          ))}
        </div>
      </div>

      {/* ② 日限 / 配额 */}
      <div className="flex flex-wrap gap-4 items-end">
        <label className="text-[12px] text-foreground">
          试用日限(次/天)
          <input
            type="number"
            min={0}
            max={100}
            aria-label="试用日限"
            value={dailyLimit}
            onChange={(e) => setDailyLimit(Number(e.target.value))}
            className="ml-2 w-20 h-7 rounded border border-border/60 bg-transparent px-2 text-[12px]"
          />
          <span className="text-[10px] text-muted-foreground ml-1">默认 {d.trial_daily_limit}，0=不给试用</span>
        </label>
        <label className="text-[12px] text-foreground">
          member 自选上限
          <input
            type="number"
            min={0}
            aria-label="自选上限"
            value={watchMax}
            onChange={(e) => setWatchMax(Number(e.target.value))}
            className="ml-2 w-20 h-7 rounded border border-border/60 bg-transparent px-2 text-[12px]"
          />
        </label>
        <label className="text-[12px] text-foreground">
          member 预警上限
          <input
            type="number"
            min={0}
            aria-label="预警上限"
            value={alertMax}
            onChange={(e) => setAlertMax(Number(e.target.value))}
            className="ml-2 w-20 h-7 rounded border border-border/60 bg-transparent px-2 text-[12px]"
          />
        </label>
      </div>

      {/* ③ 外部 skill 档位 */}
      <div>
        <div className="text-[12px] font-medium text-foreground mb-2">外部 Skill 最低档位</div>
        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-2">
          {proSkills.map((s) => (
            <label key={s.name} className="flex items-center justify-between gap-2 text-[11px] text-foreground">
              <span className="truncate" title={s.name}>{s.name}</span>
              <select
                aria-label={`档位-${s.name}`}
                value={skillTier[s.name] ?? s.builtin_tier}
                onChange={(e) =>
                  setSkillTier((m) => {
                    const next = { ...m }
                    if (e.target.value === s.builtin_tier) delete next[s.name]
                    else next[s.name] = e.target.value
                    return next
                  })
                }
                className={`h-6 rounded border bg-transparent px-1 text-[11px] ${
                  s.effective_tier === 'pro' ? 'border-amber-500/50' : 'border-border/60'
                }`}
              >
                <option value="free">free</option>
                <option value="trial">trial</option>
                <option value="pro">pro</option>
              </select>
            </label>
          ))}
        </div>
        <p className="text-[10px] text-muted-foreground mt-2 flex items-center gap-1">
          <Lock className="w-3 h-3" /> 与代码内置档位一致时不写覆盖（改回内置值 = 删除覆盖项）。
        </p>
      </div>
    </div>
  )
}
