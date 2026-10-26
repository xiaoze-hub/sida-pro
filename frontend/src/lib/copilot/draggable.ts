/**
 * 助手悬浮球拖拽的纯逻辑（无 DOM 依赖，便于单测）。
 *
 * 产品要求（2026-10-26）：球不再固定右下角，改成**全屏自由拖动**，位置记住。
 * 约束：
 *  ① 球必须始终完整留在视口内 —— 拖到边缘时贴边而不是被裁掉/消失（用户找不回来是最差体验）；
 *  ② 视口变化（旋转平板、软键盘弹出）后位置要重新夹一次，否则会跑到屏幕外；
 *  ③ 拖拽与点击必须区分 —— 手指移动 4px 以内算点击，否则算拖拽，避免"想打开却只是碰了一下"。
 */

export type Point = { x: number; y: number }
export type Size = { w: number; h: number }

/** 位置持久化键。`sida:` 前缀，与旧 `panwatch_*` 键分开，不互相覆盖。 */
export const LAUNCHER_POS_KEY = 'sida:copilot:launcher-pos'

/** 拖拽判定阈值（CSS px）。低于它就当点击。 */
export const DRAG_THRESHOLD = 4

/** 球与视口边缘的最小留白，保证点击热区和视觉呼吸。 */
export const EDGE_MARGIN = 8

/**
 * 把点位夹进视口。size 是球自身尺寸，viewport 是可视区尺寸。
 * 视口比球还小（极端窄屏）时退化为左上角 margin，不返回负数。
 */
export function clampToViewport(point: Point, size: Size, viewport: Size, margin = EDGE_MARGIN): Point {
  const maxX = Math.max(margin, viewport.w - size.w - margin)
  const maxY = Math.max(margin, viewport.h - size.h - margin)
  return {
    x: Math.min(Math.max(point.x, margin), maxX),
    y: Math.min(Math.max(point.y, margin), maxY),
  }
}

/** 位移是否超过拖拽阈值。 */
export function isDragGesture(start: Point, end: Point, threshold = DRAG_THRESHOLD): boolean {
  return Math.abs(end.x - start.x) > threshold || Math.abs(end.y - start.y) > threshold
}

/** 默认位置：右下角。按视口算，才能在平板/手机上都不跑偏。 */
export function defaultLauncherPoint(size: Size, viewport: Size, margin = EDGE_MARGIN): Point {
  return clampToViewport(
    { x: viewport.w - size.w - margin * 2, y: viewport.h - size.h - margin * 10 },
    size,
    viewport,
    margin,
  )
}

type ReadableStorage = Pick<Storage, 'getItem'>
type WritableStorage = Pick<Storage, 'setItem'>

/** 读回上次位置；任何异常/脏数据都返回 null，回落到默认位置。 */
export function readLauncherPos(storage: ReadableStorage | null | undefined): Point | null {
  if (!storage) return null
  try {
    const raw = storage.getItem(LAUNCHER_POS_KEY)
    if (!raw) return null
    const parsed = JSON.parse(raw) as Partial<Point>
    if (typeof parsed?.x !== 'number' || typeof parsed?.y !== 'number') return null
    if (!Number.isFinite(parsed.x) || !Number.isFinite(parsed.y)) return null
    return { x: parsed.x, y: parsed.y }
  } catch {
    return null
  }
}

/** 写位置。隐私模式/配额满时静默失败——位置记不住不该影响主流程。 */
export function writeLauncherPos(storage: WritableStorage | null | undefined, point: Point): void {
  if (!storage) return
  try {
    storage.setItem(LAUNCHER_POS_KEY, JSON.stringify({ x: Math.round(point.x), y: Math.round(point.y) }))
  } catch {
    /* ignore */
  }
}

/**
 * 气泡朝向：球在屏幕右半边时气泡朝左，左半边时朝右，避免气泡被屏幕边缘裁掉。
 */
export function bubbleSide(point: Point, viewport: Size): 'left' | 'right' {
  return point.x + 1 > viewport.w / 2 ? 'left' : 'right'
}
