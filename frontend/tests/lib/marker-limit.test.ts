/**
 * L2-B 钉子（2026-10-24）：K 线形态标注只留最近 3 个
 *
 * 审计实测：一根柱附近会叠 4~6 个 10px 标注（红三兵/多方炮/弹簧/假突破…），互相压住不可读。
 * 这里锁死两件事：①只留最近 N 个；②返回仍**升序**（markers 同样要求时间有序）。
 */
import { describe, expect, it } from 'vitest'
import { latestMarkers } from '@sida/biz-ui/lib/marker-limit'

const m = (time: number, text = String(time)) => ({ time: time as never, text })

describe('latestMarkers', () => {
  it('超过上限时只留最近 3 个', () => {
    const out = latestMarkers([m(1), m(2), m(3), m(4), m(5)], 3)
    expect(out.map((x) => x.time)).toEqual([3, 4, 5])
  })

  it('返回仍按时间升序（不能 reverse）', () => {
    const out = latestMarkers([m(5), m(1), m(4), m(2), m(3)], 2)
    expect(out.map((x) => x.time)).toEqual([4, 5])
  })

  it('不足上限时原样返回（引用不变，避免无谓重渲染）', () => {
    const input = [m(1), m(2)]
    expect(latestMarkers(input, 3)).toBe(input)
  })

  it('空数组安全', () => {
    expect(latestMarkers([], 3)).toEqual([])
  })

  it('保留标注自身的其它字段（text/shape 不被丢）', () => {
    const out = latestMarkers([m(1, 'a'), m(2, 'b'), m(3, 'c'), m(4, 'd')], 2)
    expect(out.map((x) => x.text)).toEqual(['c', 'd'])
  })
})
