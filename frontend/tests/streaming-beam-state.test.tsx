// @vitest-environment jsdom
import { useState } from 'react'
import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { StreamingBeam } from '../src/components/motion/StreamingBeam'

function Panel() {
  const [open, setOpen] = useState(false)
  return <button onClick={() => setOpen(!open)}>{open ? '已展开' : '已收起'}</button>
}

describe('StreamingBeam', () => {
  it('流式结束时保留子组件的展开状态', () => {
    const { rerender } = render(<StreamingBeam active><Panel /></StreamingBeam>)
    fireEvent.click(screen.getByRole('button'))
    expect(screen.getByText('已展开')).toBeTruthy()
    rerender(<StreamingBeam active={false}><Panel /></StreamingBeam>)
    expect(screen.getByText('已展开')).toBeTruthy()
  })
})
