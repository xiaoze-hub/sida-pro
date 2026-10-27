/**
 * 轻量 Lottie 场景加载器（2026-10-27）。
 *
 * 设计取舍：
 *  · **按需 import lottie-web**：它 ~250KB，绝不能进首屏主 chunk；这里和
 *    `assistant-avatar.ts` 用同一套「动态 import + 资产缓存 + 结构化克隆」模式。
 *  · **资产体积可控**：三个场景由 `scripts/gen-lotion-scenes.py` 自绘，各 1.6~4KB，
 *    不依赖外部素材站，配色/线宽能按主题 token 对齐。
 *  · **主题染色**：JSON 里颜色一律是中性灰并带 `nm: "token:*"`，加载时按传入色值替换，
 *    所以同一份资产在浅色/深色下都对，不需要两套文件。
 */
import type { AnimationItem } from 'lottie-web'

export type LotionScene = 'scan' | 'check' | 'grow'

export const LOTION_SCENES: Record<LotionScene, { src: string; loop: boolean }> = {
  // 刷新中 / 扫描中：三圈脉冲，循环
  scan: { src: '/lotion/scan.json', loop: true },
  // 成功反馈：描圈 + 描勾，**只播一次**（留在末帧）
  check: { src: '/lotion/check.json', loop: false },
  // 空态折线生长：播一次后停住，避免空页面上有东西一直动
  grow: { src: '/lotion/grow.json', loop: false },
}

type RawScene = { layers?: unknown[]; fr?: number; op?: number } & Record<string, unknown>

const cache = new Map<LotionScene, Promise<RawScene>>()

async function fetchScene(id: LotionScene): Promise<RawScene> {
  const spec = LOTION_SCENES[id]
  if (!cache.has(id)) {
    cache.set(id, fetch(spec.src)
      .then(async response => {
        if (!response.ok) throw new Error(`Lottie 资产不可用: ${spec.src}`)
        const data = (await response.json()) as RawScene
        if (!Array.isArray(data.layers)) throw new Error(`Lottie 结构非法: ${spec.src}`)
        return data
      })
      .catch(error => {
        cache.delete(id)
        throw error
      }))
  }
  return cache.get(id)!
}

/** 把 CSS 颜色解析成 Lottie 的 [r,g,b,a]（0~1）。解析不了就返回 null，保持资产原色。 */
export function parseColor(input: string | null | undefined): [number, number, number, number] | null {
  if (!input) return null
  const value = input.trim().toLowerCase()
  if (value === 'transparent' || value === 'currentcolor') return null
  const hex = /^#([0-9a-f]{3,8})$/.exec(value)
  if (hex) {
    const raw = hex[1]
    const expand = raw.length === 3 || raw.length === 4 ? raw.split('').map(char => char + char).join('') : raw
    if (expand.length !== 6 && expand.length !== 8) return null
    const r = parseInt(expand.slice(0, 2), 16) / 255
    const g = parseInt(expand.slice(2, 4), 16) / 255
    const b = parseInt(expand.slice(4, 6), 16) / 255
    const a = expand.length === 8 ? parseInt(expand.slice(6, 8), 16) / 255 : 1
    return [r, g, b, a]
  }
  const rgb = /^rgba?\(([^)]+)\)$/.exec(value)
  if (!rgb) return null
  const parts = rgb[1].split(/[,/\s]+/).filter(Boolean).map(Number)
  if (parts.length < 3 || parts.slice(0, 3).some(Number.isNaN)) return null
  return [parts[0] / 255, parts[1] / 255, parts[2] / 255, Number.isFinite(parts[3]) ? parts[3] : 1]
}

/** 递归把 `nm: "token:*"` 的描边/填充色替换成目标色（深拷贝后的副本上操作）。 */
function tint(node: unknown, color: [number, number, number, number]): void {
  if (Array.isArray(node)) {
    node.forEach(child => tint(child, color))
    return
  }
  if (!node || typeof node !== 'object') return
  const record = node as Record<string, unknown>
  const name = typeof record.nm === 'string' ? record.nm : ''
  const isPaint = record.ty === 'st' || record.ty === 'fl'
  if (isPaint && name.startsWith('token:') && record.c && typeof record.c === 'object') {
    const paint = record.c as Record<string, unknown>
    paint.a = 0
    paint.k = color
  }
  for (const value of Object.values(record)) tint(value, color)
}

export type LoadedScene = { lottie: typeof import('lottie-web')['default']; data: RawScene; loop: boolean }

export async function loadLotionScene(id: LotionScene, color?: string | null): Promise<LoadedScene> {
  const raw = await fetchScene(id)
  const { default: lottie } = await import('lottie-web')
  // Lottie 会就地改写数据（trim/repeater），每个实例必须独立副本。
  const data = structuredClone(raw)
  const parsed = parseColor(color)
  if (parsed) tint(data.layers, parsed)
  return { lottie, data, loop: LOTION_SCENES[id].loop }
}

export type { AnimationItem }
