// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'
import { TabActiveProvider } from '@panwatch/base-ui'
import type { SelfCheckItem, SystemStatusSnapshot } from '@panwatch/api'
import { withProbeResults } from '@/lib/system-status'
import { SystemTopology } from '@/components/system/SystemTopology'

const snapshot: SystemStatusSnapshot = { checked_at: '2026-10-26T12:00:00Z', version: 'test',
  nodes: ['api', 'database', 'datasource', 'ai', 'notify'].map(id => ({ id, label: id, kind: id, status: id === 'api' || id === 'database' ? 'ok' : 'unknown', detail: '测试状态', connected: id === 'api' || id === 'database', latency_ms: null, items: [] })),
  edges: ['database', 'datasource', 'ai', 'notify'].map(id => ({ id: `api-${id}`, source: 'api', target: id, connected: id === 'database' })) }
const probe = (category: SelfCheckItem['category'], status: SelfCheckItem['status'], note: string | null = null): SelfCheckItem => ({ category, key: `${category}:1`, name: category, status, latency_ms: 1, hint: '', error: null, note, group: null })
afterEach(cleanup)

describe('系统连接状态诚实性', () => {
  it('数据源或模型只有主动检测成功才流动，混合失败不显示全通', () => {
    const data = withProbeResults(snapshot, [probe('ai', 'ok'), probe('datasource', 'ok'), { ...probe('datasource', 'fail'), key: 'datasource:2' }])
    expect(data.edges.find(edge => edge.target === 'ai')?.connected).toBe(true)
    expect(data.edges.find(edge => edge.target === 'datasource')?.connected).toBe(false)
    expect(data.nodes.find(node => node.id === 'datasource')?.status).toBe('slow')
    expect(snapshot.nodes.find(node => node.id === 'ai')?.status).toBe('unknown')
  })
  it('通知只校验配置不能显示投递连通', () => {
    const data = withProbeResults(snapshot, [probe('notify', 'ok', '仅校验配置，未真实发送')])
    expect(data.nodes.find(node => node.id === 'notify')?.status).toBe('configured')
    expect(data.edges.find(edge => edge.target === 'notify')?.connected).toBe(false)
  })
  it('流动仅挂在连通线路，错误或隐藏页签暂停，并可键盘点击详情', () => {
    let selected = ''
    const view = render(<SystemTopology data={snapshot} onSelect={node => { selected = node.id }} />)
    expect(view.container.querySelectorAll('.system-connection-flow').length).toBe(1)
    fireEvent.click(screen.getByRole('button', { name: 'database：正常，查看详情' }))
    expect(selected).toBe('database')
    view.rerender(<SystemTopology data={snapshot} paused onSelect={() => {}} />)
    expect(view.container.querySelector('.system-connection-flow')).toBeNull()
    view.rerender(<TabActiveProvider active={false}><SystemTopology data={snapshot} onSelect={() => {}} /></TabActiveProvider>)
    expect(view.container.querySelector('[data-flow-active="false"]')).toBeTruthy()
  })
})
