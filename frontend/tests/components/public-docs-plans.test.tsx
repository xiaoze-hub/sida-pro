// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import Tiers from '@/pages/Tiers'
import Developers from '@/pages/Developers'
import { COMMUNITY_REPOSITORY, PROJECT_REPOSITORY } from '@/lib/product-links'

const api = vi.hoisted(() => ({ fetchTiers: vi.fn(), fetchProApplyStatus: vi.fn(), applyPro: vi.fn(), isAuthenticated: vi.fn(), fetchAPI: vi.fn(), getMyPermissions: vi.fn(), getToken: vi.fn() }))
vi.mock('@panwatch/api', () => api)
vi.mock('@/hooks/useI18n', () => ({ useI18n: () => ({ t: (key: string) => key === 'developers.title' ? '开发者文档' : key }) }))
const tiers = {
  tiers: [
    { key: 'member', label: '免费账户', count: 1, groups: [{ group: '基础能力', items: ['后端提供的基础权限'] }] },
    { key: 'pro', label: 'Pro 账户', count: 2, groups: [{ group: '扩展能力', items: ['后端提供的基础权限', '后端提供的扩展权限'] }] },
  ],
  member_limits: { watchlist_max: 0, alert_max: 4, trial_daily_limit: 7 },
  billing_enabled: false, note: '实例实时说明', limits_note: '来自管理员配置', demo_note: '以实际权限为准',
}
const catalog = { skills: [
  { name: 'get_stock_quote', description: '读取行情', tier_min: 'free', slow: false, caliber: 'tick', params: { symbol: { type: 'string', default: '000001' } }, required: ['symbol'] },
  { name: 'other_tool', description: '其他接口', tier_min: 'pro', slow: false, caliber: '', params: {}, required: [] },
], tiers: {}, guest: { daily_limit: 6 } }
function show(page: React.ReactNode, path = '/tiers') { return render(<MemoryRouter initialEntries={[path]}>{page}</MemoryRouter>) }
beforeEach(() => {
  vi.clearAllMocks()
  vi.stubGlobal('matchMedia', vi.fn(() => ({ matches: false, addEventListener: vi.fn(), removeEventListener: vi.fn() })))
  api.isAuthenticated.mockReturnValue(false)
  api.fetchTiers.mockResolvedValue(tiers)
  api.fetchProApplyStatus.mockResolvedValue({ role: 'member', application: null, has_application: false })
  api.applyPro.mockResolvedValue({ status: 'pending' })
  api.fetchAPI.mockResolvedValue(catalog)
  api.getMyPermissions.mockResolvedValue({ role: 'member' })
  localStorage.clear()
})
afterEach(() => { cleanup(); vi.unstubAllGlobals() })

describe('公开账户方案', () => {
  it('区分社区部署与实例账户，链接正确且额度为后端值（包括零）', async () => {
    show(<Tiers />)
    expect(screen.getByRole('link', { name: /查看开源仓库/ }).getAttribute('href')).toBe(COMMUNITY_REPOSITORY)
    expect(screen.getAllByRole('link', { name: 'GitHub 项目仓库' }).every(link => link.getAttribute('href') === PROJECT_REPOSITORY)).toBe(true)
    expect(screen.getByRole('heading', { name: '社区开源版 · 免费自行部署' })).toBeTruthy()
    expect(await screen.findByText('后端提供的扩展权限')).toBeTruthy()
    expect(screen.getByText('0').textContent).toBe('0只')
    expect(screen.getByRole('link', { name: '注册免费账户' }).getAttribute('href')).toBe('/login?mode=register')
    expect(api.fetchProApplyStatus).not.toHaveBeenCalled()
    expect(api.applyPro).not.toHaveBeenCalled()
  })
  it('接口失败时仍可访问社区仓库，重试恢复真实方案', async () => {
    api.fetchTiers.mockRejectedValueOnce(new Error('offline'))
    show(<Tiers />)
    await screen.findByRole('alert')
    expect(screen.getByRole('link', { name: /查看开源仓库/ })).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: '重新读取方案' }))
    expect(await screen.findByText('后端提供的扩展权限')).toBeTruthy()
    expect(api.fetchTiers).toHaveBeenCalledTimes(2)
  })
  it('owner 无申请记录仍视为已开通，登录后不叠加官网导航', async () => {
    api.isAuthenticated.mockReturnValue(true)
    api.fetchProApplyStatus.mockResolvedValue({ role: 'owner', application: null })
    show(<Tiers />)
    expect(await screen.findByText('已开通 Pro')).toBeTruthy()
    expect(screen.queryByRole('button', { name: '申请 Pro' })).toBeNull()
    expect(screen.queryByRole('navigation', { name: '官网导航' })).toBeNull()
    expect(api.applyPro).not.toHaveBeenCalled()
  })
  it('提交成功但状态刷新失败时仍显示审核中，防止重复提交', async () => {
    api.isAuthenticated.mockReturnValue(true)
    api.fetchProApplyStatus.mockResolvedValueOnce({ role: 'member', application: null }).mockRejectedValueOnce(new Error('offline'))
    show(<Tiers />)
    fireEvent.click(await screen.findByRole('button', { name: '申请 Pro' }))
    const button = await screen.findByRole('button', { name: '申请审核中' })
    expect((button as HTMLButtonElement).disabled).toBe(true)
    expect(screen.getByText('申请已提交，等待管理员审核。')).toBeTruthy()
    expect(api.applyPro).toHaveBeenCalledTimes(1)
  })
  it('账户状态读取失败时先重试，不把未知账户当作可申请', async () => {
    api.isAuthenticated.mockReturnValue(true)
    api.fetchProApplyStatus.mockRejectedValueOnce(new Error('offline'))
    show(<Tiers />)
    const retry = await screen.findByRole('button', { name: '重试读取账户状态' })
    expect(screen.queryByRole('button', { name: '申请 Pro' })).toBeNull()
    fireEvent.click(retry)
    await screen.findByRole('button', { name: '申请 Pro' })
  })
})

describe('公开开发者文档', () => {
  it('分类只显示当前章节，目录可搜索并将所选接口带入调试台', async () => {
    show(<Developers />, '/developers')
    await waitFor(() => expect(api.fetchAPI).toHaveBeenCalled())
    expect(screen.getByRole('navigation', { name: '官网导航' })).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: 'Skill 目录' }))
    fireEvent.change(await screen.findByRole('searchbox', { name: '搜索 Skill 目录' }), { target: { value: 'get_stock_quote' } })
    expect(screen.queryByText('other_tool')).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: '调试此接口' }))
    expect(screen.getByRole('heading', { name: '在线调试台' })).toBeTruthy()
    expect(screen.queryByRole('searchbox')).toBeNull()
    expect((screen.getByRole('textbox', { name: '参数 JSON' }) as HTMLTextAreaElement).value).toContain('000001')
    const fetch = vi.fn(); vi.stubGlobal('fetch', fetch)
    fireEvent.click(screen.getByRole('button', { name: '运行' }))
    await screen.findByText('请先填写 API Key，或切换为游客模式。')
    expect(fetch).not.toHaveBeenCalled()
  })
  it('开源部署分类给出真实仓库与许可，复制失败明确提示', async () => {
    Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText: vi.fn().mockRejectedValue(new Error('denied')) } })
    show(<Developers />, '/developers?section=sec-selfhost')
    expect(screen.getByRole('heading', { name: '在自己的环境中部署 SIDA' })).toBeTruthy()
    expect(screen.getByRole('link', { name: /xiaoze-hub\/Stock-Intelligent/ }).getAttribute('href')).toBe(COMMUNITY_REPOSITORY)
    fireEvent.click(screen.getByRole('button', { name: '复制 bash 代码' }))
    expect(await screen.findByRole('status')).toHaveProperty('textContent', '复制未完成，请选择代码后手动复制。')
    expect(screen.queryByText('已复制')).toBeNull()
  })
})
