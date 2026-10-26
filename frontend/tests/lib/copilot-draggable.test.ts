import { describe, expect, it, vi } from 'vitest'
import {
  DRAG_THRESHOLD,
  EDGE_MARGIN,
  LAUNCHER_POS_KEY,
  bubbleSide,
  clampToViewport,
  defaultLauncherPoint,
  isDragGesture,
  readLauncherPos,
  writeLauncherPos,
} from '@/lib/copilot/draggable'

const BALL = { w: 96, h: 96 }

describe('clampToViewport', () => {
  it('把越界的点拉回视口内', () => {
    const p = clampToViewport({ x: -50, y: -50 }, BALL, { w: 1200, h: 800 })
    expect(p).toEqual({ x: EDGE_MARGIN, y: EDGE_MARGIN })
  })

  it('右下越界时贴边而不是溢出', () => {
    const p = clampToViewport({ x: 5000, y: 5000 }, BALL, { w: 1200, h: 800 })
    expect(p).toEqual({ x: 1200 - 96 - EDGE_MARGIN, y: 800 - 96 - EDGE_MARGIN })
  })

  it('视口比球还小时不返回负数坐标', () => {
    const p = clampToViewport({ x: 10, y: 10 }, BALL, { w: 60, h: 60 })
    expect(p.x).toBeGreaterThanOrEqual(EDGE_MARGIN)
    expect(p.y).toBeGreaterThanOrEqual(EDGE_MARGIN)
  })

  it('正常范围内的点原样返回', () => {
    expect(clampToViewport({ x: 300, y: 400 }, BALL, { w: 1200, h: 800 })).toEqual({ x: 300, y: 400 })
  })
})

describe('isDragGesture', () => {
  it('位移在阈值内算点击', () => {
    expect(isDragGesture({ x: 100, y: 100 }, { x: 102, y: 101 })).toBe(false)
  })

  it('超过阈值算拖拽（含单轴）', () => {
    expect(isDragGesture({ x: 100, y: 100 }, { x: 100 + DRAG_THRESHOLD + 1, y: 100 })).toBe(true)
    expect(isDragGesture({ x: 100, y: 100 }, { x: 100, y: 100 - DRAG_THRESHOLD - 1 })).toBe(true)
  })
})

describe('defaultLauncherPoint', () => {
  it('默认落在右下角且完整可见', () => {
    const p = defaultLauncherPoint(BALL, { w: 1280, h: 800 })
    expect(p.x).toBeGreaterThan(1280 / 2)
    expect(p.y).toBeGreaterThan(800 / 2)
    expect(p.x + BALL.w).toBeLessThanOrEqual(1280)
    expect(p.y + BALL.h).toBeLessThanOrEqual(800)
  })

  it('平板竖屏也留得住（不被裁）', () => {
    const p = defaultLauncherPoint(BALL, { w: 768, h: 1024 })
    expect(p.x + BALL.w).toBeLessThanOrEqual(768)
    expect(p.y + BALL.h).toBeLessThanOrEqual(1024)
  })

  it('桌面端默认贴右下 20px，与旧版 bottom-5/right-5 一致（不许自己拍数）', () => {
    const p = defaultLauncherPoint(BALL, { w: 1600, h: 1000 })
    expect(1600 - (p.x + BALL.w)).toBe(20)
    expect(1000 - (p.y + BALL.h)).toBe(20)
  })

  it('手机端默认抬高到底部导航之上（bottom 80 / right 16）', () => {
    const p = defaultLauncherPoint(BALL, { w: 390, h: 844 }, { right: 16, bottom: 80 })
    expect(390 - (p.x + BALL.w)).toBe(16)
    expect(844 - (p.y + BALL.h)).toBe(80)
  })
})

describe('持久化', () => {
  it('读写往返一致', () => {
    const store = new Map<string, string>()
    const storage = {
      getItem: (k: string) => store.get(k) ?? null,
      setItem: (k: string, v: string) => void store.set(k, v),
    }
    writeLauncherPos(storage, { x: 12.4, y: 33.6 })
    expect(JSON.parse(store.get(LAUNCHER_POS_KEY)!)).toEqual({ x: 12, y: 34 })
    expect(readLauncherPos(storage)).toEqual({ x: 12, y: 34 })
  })

  it('脏数据回落到 null（不能把球扔到 Infinity）', () => {
    const storage = { getItem: () => '{"x":"oops","y":1}' }
    expect(readLauncherPos(storage)).toBeNull()
  })

  it('不可解析的 JSON 不抛异常', () => {
    expect(readLauncherPos({ getItem: () => 'not json' })).toBeNull()
  })

  it('storage 抛异常时静默失败', () => {
    const boom = { getItem: vi.fn(() => { throw new Error('denied') }), setItem: vi.fn(() => { throw new Error('denied') }) }
    expect(readLauncherPos(boom)).toBeNull()
    expect(() => writeLauncherPos(boom, { x: 1, y: 2 })).not.toThrow()
  })
})

describe('bubbleSide', () => {
  it('右半边朝左、左半边朝右', () => {
    expect(bubbleSide({ x: 900, y: 10 }, { w: 1200, h: 800 })).toBe('left')
    expect(bubbleSide({ x: 100, y: 10 }, { w: 1200, h: 800 })).toBe('right')
  })
})
