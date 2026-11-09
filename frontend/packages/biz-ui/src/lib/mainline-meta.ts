/**
 * 主线数据时点 / 缓存时效文案(2026-11-09 复审修复)。
 *
 * 背景: `/api/market/mainline` 的契约里本来就有
 *   data_meta.as_of   —— 涨停池的**数据交易日**(后端拿不到就 null, 不猜成今天)
 *   data_meta.as_of_note —— 为什么没有交易日
 *   stale / age_s     —— SWR 读侧口径: 返的是不是旧值、载荷多少秒前算的
 * 但前端只显示了「本次拉取的墙钟」, 于是用户无法判断这张榜是哪天的、是不是旧快照。
 *
 * 这里集中一处口径, 供主线卡(行业/概念/并列/空态)与首页来源标签共用:
 *   - as_of 缺失 → 明写「时点未知」, 绝不拿今天/墙钟顶上;
 *   - stale 缺失(旧后端/旧缓存载荷) → 写「缓存时效未知」, 缺契约 ≠ 新鲜;
 *   - age_s 有值 → 给人读的「N 秒/分钟/小时前」, 不写死「实时」。
 */

/** ISO 日期(可带时间) → 'YYYY-MM-DD'; 其它一律 null(不猜) */
export function mainlineAsOfDate(v?: string | null): string | null {
  if (typeof v !== 'string') return null
  const m = v.trim().match(/^(\d{4})-(\d{2})-(\d{2})/)
  if (!m) return null
  const [, y, mo, d] = m
  const iso = `${y}-${mo}-${d}`
  // 反向校验: 2026-02-31 这类非法日历日不认
  const parsed = new Date(`${iso}T00:00:00Z`)
  if (Number.isNaN(parsed.getTime()) || parsed.toISOString().slice(0, 10) !== iso) return null
  return iso
}

/** 数据日展示: '2026-11-06' → '11-06'; 缺失 → '时点未知' */
export function mainlineAsOfText(v?: string | null): string {
  const iso = mainlineAsOfDate(v)
  return iso ? iso.slice(5) : '时点未知'
}

/** 秒 → 人读年龄(秒/分钟/小时), 不用 toFixed(UI 规则 R6) */
function humanAge(ageS: number): string {
  const s = Math.max(0, ageS)
  if (s < 60) return `${Math.round(s)} 秒前`
  if (s < 3600) return `${Math.round(s / 60)} 分钟前`
  return `${Math.round(s / 360) / 10} 小时前`
}

/**
 * 缓存时效文案。
 *   stale=true      → '旧数据 · 12 分钟前' / '旧数据 · 缓存时效未知'
 *   stale=false     → '缓存 43 秒前' (age 缺失 → null: 无依据不写结论)
 *   stale 缺失      → '缓存时效未知(数据 43 秒前)' —— 契约缺失不等于新鲜
 */
export function mainlineCacheText(
  stale?: boolean | null,
  ageS?: number | null,
): string | null {
  const age = typeof ageS === 'number' && Number.isFinite(ageS) ? Math.max(0, ageS) : null
  if (stale === true) {
    return age == null ? '旧数据 · 缓存时效未知' : `旧数据 · ${humanAge(age)}`
  }
  if (stale === false) {
    return age == null ? null : `缓存 ${humanAge(age)}`
  }
  return age == null ? '缓存时效未知' : `缓存时效未知（数据 ${humanAge(age)}）`
}

/**
 * 首页「结论来源」标签里的主线日期/缓存片段。
 *   有数据日 → '数据日 11-06'; 后端没给 → '日期未获取'; 首拉中 → '数据日获取中'
 * 绝不把「日期未获取」写成静态文案 —— 那份日期后端其实给了就必须显示。
 */
export function mainlineSourceLine(input: {
  asOf?: string | null
  stale?: boolean | null
  ageS?: number | null
  loading?: boolean
}): string {
  const iso = mainlineAsOfDate(input.asOf)
  if (!iso && input.loading) return '数据日获取中'
  const dateText = iso ? `数据日 ${iso.slice(5)}` : '日期未获取'
  const cache = mainlineCacheText(input.stale, input.ageS)
  return cache ? `${dateText} · ${cache}` : dateText
}
