import { useCallback, useEffect, useState } from 'react'
import { RefreshCw } from 'lucide-react'
import { authApi } from '@sida/api'
import { Input } from '@sida/base-ui/components/ui/input'
import { Label } from '@sida/base-ui/components/ui/label'

export interface CaptchaValue {
  id: string | null
  code: string
}

interface Props {
  value: CaptchaValue
  onChange: (next: CaptchaValue) => void
  /** 父组件每次登录失败把这个值 +1，强制换一张图（旧图已作废）。 */
  refreshSignal?: number
  disabled?: boolean
}

/**
 * 登录验证码（2026-10-31）。
 *
 * 服务端默认要求验证码（`AUTH_CAPTCHA_MODE=always`），机器身份走 `X-Service-Token` 豁免。
 * 这里只做三件事：取图、收输入、换一张。**答错必然作废**——服务端一次性消费，
 * 所以父组件在登录失败时把 `refreshSignal` +1，别让用户拿旧图重试。
 */
export default function CaptchaField({ value, onChange, refreshSignal = 0, disabled }: Props) {
  const [image, setImage] = useState<string | null>(null)
  const [enabled, setEnabled] = useState<boolean | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const load = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const data = await authApi.captcha()
      if (!data.enabled || !data.image) {
        setEnabled(false)
        setImage(null)
        onChange({ id: null, code: '' })
        return
      }
      setEnabled(true)
      setImage(data.image)
      onChange({ id: data.captcha_id, code: '' })
    } catch (e) {
      // 拿不到验证码就明确说不可用：绝不放行"没验证码也能登录"
      setEnabled(true)
      setImage(null)
      setError(e instanceof Error ? e.message : '验证码加载失败')
    } finally {
      setLoading(false)
    }
    // onChange 由父组件提供，故意不进依赖，避免每次渲染都重新取图
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [refreshSignal])

  useEffect(() => {
    void load()
  }, [load])

  if (enabled === false) return null

  return (
    <div data-testid="captcha-field">
      <Label htmlFor="login-captcha" className="mb-1 block text-[14px] text-muted-foreground">
        验证码
      </Label>
      <div className="flex items-center gap-2">
        {image ? (
          // 图片是后端生成的 data URI，不是装饰图；alt 说明用途，便于读屏
          <img
            src={image}
            alt="图片验证码，点击右侧按钮可换一张"
            className="h-11 w-[132px] rounded bg-card"
          />
        ) : (
          <div className="flex h-11 w-[132px] items-center justify-center rounded bg-card text-[12px] text-muted-foreground">
            {loading ? '加载中…' : '不可用'}
          </div>
        )}
        <button
          type="button"
          onClick={() => void load()}
          disabled={loading || disabled}
          aria-label="换一张验证码"
          className="rounded p-2 text-muted-foreground hover:text-foreground disabled:opacity-50"
        >
          <RefreshCw className={loading ? 'h-4 w-4 animate-spin' : 'h-4 w-4'} />
        </button>
        <Input
          id="login-captcha"
          value={value.code}
          onChange={(e) => onChange({ id: value.id, code: e.target.value })}
          placeholder="看图输入"
          autoComplete="off"
          inputMode="text"
          maxLength={8}
          disabled={disabled || !image}
          aria-label="验证码答案"
        />
      </div>
      {error ? <p className="mt-1 text-[12px] text-role-risk">{error}</p> : null}
    </div>
  )
}
