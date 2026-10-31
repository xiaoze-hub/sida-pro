// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import CaptchaField from '@/components/auth/CaptchaField'

const captchaMock = vi.fn()
vi.mock('@sida/api', () => ({
  authApi: { captcha: () => captchaMock() },
}))

const IMAGE = 'data:image/png;base64,iVBORw0KGgo='

describe('CaptchaField', () => {
  beforeEach(() => {
    captchaMock.mockReset()
  })
  afterEach(() => cleanup())

  it('取到图后把 captcha_id 交给父组件，并且不预填答案', async () => {
    const onChange = vi.fn()
    captchaMock.mockResolvedValue({
      enabled: true, captcha_id: 'abc.123.sig', image: IMAGE, expires_in: 180,
    })
    render(<CaptchaField value={{ id: null, code: '' }} onChange={onChange} />)
    await waitFor(() => expect(screen.getByAltText(/图片验证码/)).toBeTruthy())
    expect(onChange).toHaveBeenCalledWith({ id: 'abc.123.sig', code: '' })
  })

  it('服务端说不需要验证码时什么都不渲染（不做假安全）', async () => {
    captchaMock.mockResolvedValue({
      enabled: false, captcha_id: null, image: null, expires_in: 0,
    })
    const { container } = render(<CaptchaField value={{ id: null, code: '' }} onChange={() => {}} />)
    await waitFor(() => expect(captchaMock).toHaveBeenCalled())
    expect(container.querySelector('[data-testid="captcha-field"]')).toBeNull()
  })

  it('输入走 onChange，id 保持不变', async () => {
    const onChange = vi.fn()
    captchaMock.mockResolvedValue({
      enabled: true, captcha_id: 'id-1', image: IMAGE, expires_in: 180,
    })
    render(<CaptchaField value={{ id: 'id-1', code: '' }} onChange={onChange} />)
    const input = await screen.findByLabelText('验证码答案')
    fireEvent.change(input, { target: { value: 'AB2CD' } })
    expect(onChange).toHaveBeenCalledWith({ id: 'id-1', code: 'AB2CD' })
  })

  it('换一张会重新取图（旧码一次性，答错必须换）', async () => {
    captchaMock.mockResolvedValue({
      enabled: true, captcha_id: 'id-2', image: IMAGE, expires_in: 180,
    })
    render(<CaptchaField value={{ id: 'id-2', code: '' }} onChange={() => {}} />)
    await screen.findByAltText(/图片验证码/)
    fireEvent.click(screen.getByLabelText('换一张验证码'))
    await waitFor(() => expect(captchaMock).toHaveBeenCalledTimes(2))
  })

  it('取图失败要明说不可用，而不是留一个能提交的空表单', async () => {
    captchaMock.mockRejectedValue(new Error('验证码服务不可用，请稍后重试'))
    render(<CaptchaField value={{ id: null, code: '' }} onChange={() => {}} />)
    expect(await screen.findByText(/验证码服务不可用/)).toBeTruthy()
    expect((screen.getByLabelText('验证码答案') as HTMLInputElement).disabled).toBe(true)
  })
})
