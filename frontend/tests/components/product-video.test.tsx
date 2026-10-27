// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, expect, it } from 'vitest'
import { ProductVideo } from '@/components/landing/ProductVideo'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@sida/base-ui/components/ui/tabs'

afterEach(cleanup)
const props = { title: '行情工作台', src: '/media/product/market.mp4', poster: '/media/product/market-poster.webp', captions: '/media/product/market.zh.vtt', duration: '00:31' }

it('先显示封面，主动播放后才加载带字幕的播放器', () => {
  const { container } = render(<ProductVideo {...props} />)
  expect(screen.getByAltText('行情工作台视频预览')).toBeTruthy()
  expect(container.querySelector('video')).toBeNull()
  fireEvent.click(screen.getByRole('button', { name: '播放行情工作台演示' }))
  const player = screen.getByLabelText('行情工作台操作视频') as HTMLVideoElement
  expect(player.getAttribute('src')).toBe(props.src)
  expect(player.controls).toBe(true)
  expect(player.playsInline).toBe(true)
  expect(player.querySelector('track')?.getAttribute('src')).toBe(props.captions)
})

it('封面失败保留明确操作，播放失败允许重试与下载', () => {
  render(<ProductVideo {...props} />)
  fireEvent.error(screen.getByAltText('行情工作台视频预览'))
  expect(screen.getByRole('status').textContent).toContain('封面暂时无法加载')
  fireEvent.click(screen.getByRole('button', { name: '直接播放' }))
  fireEvent.error(screen.getByLabelText('行情工作台操作视频'))
  expect(screen.getByRole('status').textContent).toContain('视频暂时无法播放')
  expect(screen.getByRole('link', { name: '下载视频' }).getAttribute('href')).toBe(props.src)
  fireEvent.click(screen.getByRole('button', { name: '重新播放' }))
  expect(screen.getByLabelText('行情工作台操作视频')).toBeTruthy()
})

it('切换真实 Tabs 卸载上一播放器，再返回展示封面', () => {
  render(<Tabs defaultValue="market"><TabsList><TabsTrigger value="market">行情</TabsTrigger><TabsTrigger value="system">系统</TabsTrigger></TabsList><TabsContent value="market"><ProductVideo {...props} /></TabsContent><TabsContent value="system">系统状态</TabsContent></Tabs>)
  fireEvent.click(screen.getByRole('button', { name: '播放行情工作台演示' }))
  expect(screen.getByLabelText('行情工作台操作视频')).toBeTruthy()
  fireEvent.mouseDown(screen.getByRole('tab', { name: '系统' }), { button: 0, ctrlKey: false })
  expect(screen.queryByLabelText('行情工作台操作视频')).toBeNull()
  fireEvent.mouseDown(screen.getByRole('tab', { name: '行情' }), { button: 0, ctrlKey: false })
  expect(screen.getByRole('button', { name: '播放行情工作台演示' })).toBeTruthy()
})
