/** W5.3: 模型功能标签徽标(从 Settings.tsx 抽出, 逐字保留)。 */

export const MODEL_CAP_META: Record<string, { label: string; badge: string }> = {
  chat: { label: '对话', badge: 'bg-emerald-500/10 text-emerald-700 dark:text-emerald-400 border-emerald-500/25' },
  vision: { label: '视觉', badge: 'bg-sky-500/10 text-sky-700 dark:text-sky-400 border-sky-500/25' },
  image: { label: '图像', badge: 'bg-purple-500/10 text-purple-700 dark:text-purple-400 border-purple-500/25' },
  video: { label: '视频', badge: 'bg-orange-500/10 text-orange-700 dark:text-orange-400 border-orange-500/25' },
  tools: { label: '工具', badge: 'bg-cyan-500/10 text-cyan-700 dark:text-cyan-400 border-cyan-500/25' },
}
export const MODEL_CAP_ORDER = ['chat', 'vision', 'image', 'video', 'tools']

export function CapBadges({ caps }: { caps?: string[] }) {
  const list = (caps || []).filter(c => MODEL_CAP_META[c]).sort((a, b) => MODEL_CAP_ORDER.indexOf(a) - MODEL_CAP_ORDER.indexOf(b))
  if (list.length === 0) return null
  return (
    <span className="inline-flex items-center gap-1">
      {list.map(c => (
        <span key={c} className={`inline-flex items-center rounded-full border px-1.5 py-px text-[10px] leading-4 ${MODEL_CAP_META[c].badge}`}>
          {MODEL_CAP_META[c].label}
        </span>
      ))}
    </span>
  )
}
