// @vitest-environment jsdom
/**
 * 后台分页条 —— 2026-10-24（交接文档第 1 项，/admin 页高 20598px 治理）。
 *
 * 只钉两件事，但都是"会骗人"的地方：
 *   ① **总数只能是全量长度**。用「本页 20 条」冒充「共 20 人」，翻到第 2 页立刻穿帮。
 *   ② 首页/末页时按钮要禁用，不能出现负数页或越界页。
 */
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import Pager from '@/components/Pager'

afterEach(cleanup)

describe('Pager', () => {
  it('总数 ≤ 一页时不显示翻页按钮，只报总数', () => {
    render(<Pager total={7} page={1} pageSize={20} onPage={() => {}} unit="人" />)
    expect(screen.getByText('共 7 人')).toBeTruthy()
    expect(screen.queryByTestId('pager-prev')).toBeNull()
    expect(screen.queryByTestId('pager-next')).toBeNull()
  })

  it('显示的是全量总数与当前区间，不是本页条数', () => {
    render(<Pager total={137} page={2} pageSize={20} onPage={() => {}} unit="人" />)
    expect(screen.getByText('21–40 / 共 137 人')).toBeTruthy()
    expect(screen.getByText('2 / 7')).toBeTruthy()
  })

  it('第一页禁用「上一页」、末页禁用「下一页」', () => {
    const { unmount } = render(<Pager total={137} page={1} pageSize={20} onPage={() => {}} />)
    expect((screen.getByTestId('pager-prev') as HTMLButtonElement).disabled).toBe(true)
    expect((screen.getByTestId('pager-next') as HTMLButtonElement).disabled).toBe(false)
    unmount()

    render(<Pager total={137} page={7} pageSize={20} onPage={() => {}} />)
    expect((screen.getByTestId('pager-next') as HTMLButtonElement).disabled).toBe(true)
  })

  it('点击翻页回调目标页号（不是增量）', () => {
    const onPage = vi.fn()
    render(<Pager total={137} page={3} pageSize={20} onPage={onPage} />)
    fireEvent.click(screen.getByTestId('pager-next'))
    expect(onPage).toHaveBeenCalledWith(4)
    fireEvent.click(screen.getByTestId('pager-prev'))
    expect(onPage).toHaveBeenCalledWith(2)
  })
})
