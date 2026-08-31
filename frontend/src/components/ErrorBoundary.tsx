import { Component, ReactNode, ErrorInfo } from 'react'
import { AlertTriangle, RefreshCw } from 'lucide-react'

interface ErrorBoundaryProps {
  children: ReactNode
  /** 错误信息展示 fallback */
  fallback?: (error: Error, reset: () => void) => ReactNode
  /** 错误发生回调(用于上报) */
  onError?: (error: Error, info: ErrorInfo) => void
}

interface ErrorBoundaryState {
  error: Error | null
}

/**
 * 错误边界 (2026-08-17 v0.2.64)
 *
 * 关闭 B 报告 P1-10: 全仓 0 个 React Error Boundary, runtime error 白屏/崩溃
 * - App 级包整个 root, 任何 subtree 抛错都降级 UI 而不是整页崩溃
 * - 每个 LazyRoute 单独包, 防止一个路由错误影响整个 App
 *
 * 用法:
 *   <AppErrorBoundary>
 *     <App />
 *   </AppErrorBoundary>
 */
export class AppErrorBoundary extends Component<ErrorBoundaryProps, ErrorBoundaryState> {
  state: ErrorBoundaryState = { error: null }

  static getDerivedStateFromError(error: Error): ErrorBoundaryState {
    return { error }
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    // 2026-08-17: 控制台 + 调用方回调(可接 Sentry 等)
    console.error('[AppErrorBoundary]', error, info)
    this.props.onError?.(error, info)
  }

  reset = () => {
    this.setState({ error: null })
  }

  render() {
    const { error } = this.state
    if (!error) return this.props.children

    if (this.props.fallback) {
      return this.props.fallback(error, this.reset)
    }

    return (
      <div className="flex flex-col items-center justify-center min-h-screen p-6 bg-background text-foreground">
        <div className="max-w-md text-center space-y-4">
          <div className="inline-flex items-center justify-center w-12 h-12 rounded-full bg-amber-500/10">
            <AlertTriangle className="w-6 h-6 text-amber-600" />
          </div>
          <div>
            <h1 className="text-lg font-semibold">页面遇到了问题</h1>
            <p className="text-sm text-muted-foreground mt-1.5">
              渲染时发生了一个意外错误, 已阻止整页崩溃。
            </p>
            {import.meta.env.DEV && (
              <pre className="mt-3 text-left text-[11px] bg-muted p-3 rounded-lg overflow-auto max-h-40">
                {error.message}
                {'\n'}
                {error.stack?.split('\n').slice(1, 6).join('\n')}
              </pre>
            )}
          </div>
          <div className="flex items-center justify-center gap-2">
            <button
              type="button"
              onClick={this.reset}
              className="inline-flex items-center gap-1.5 rounded-md px-3 py-1.5 bg-primary text-primary-foreground text-[13px] hover:bg-primary/90 transition-colors"
            >
              <RefreshCw className="w-3.5 h-3.5" />
              重试
            </button>
            <a
              href="/"
              className="inline-flex items-center gap-1.5 rounded-md px-3 py-1.5 border border-border text-[13px] hover:bg-accent transition-colors"
            >
              回到首页
            </a>
          </div>
        </div>
      </div>
    )
  }
}

export default AppErrorBoundary