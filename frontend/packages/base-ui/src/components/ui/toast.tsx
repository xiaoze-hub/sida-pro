import { useState, useEffect, createContext, useContext, useCallback, useRef } from 'react'
import { X, CheckCircle2, AlertCircle, Info } from 'lucide-react'

type ToastType = 'success' | 'error' | 'info'

interface Toast {
  id: number
  type: ToastType
  message: string
}

interface ToastContextValue {
  toast: (message: string, type?: ToastType) => void
}

const ToastContext = createContext<ToastContextValue>({ toast: () => {} })

export function useToast() {
  return useContext(ToastContext)
}

let nextId = 0

export function ToastProvider({ children }: { children: React.ReactNode }) {
  const [toasts, setToasts] = useState<Toast[]>([])
  const timersRef = useRef<Map<number, ReturnType<typeof setTimeout>>>(new Map())

  const removeToast = useCallback((id: number) => {
    setToasts(prev => prev.filter(t => t.id !== id))
    const timer = timersRef.current.get(id)
    if (timer) { clearTimeout(timer); timersRef.current.delete(id) }
  }, [])

  const toast = useCallback((message: string, type: ToastType = 'info') => {
    const id = ++nextId
    setToasts(prev => [...prev, { id, type, message }])
    const timer = setTimeout(() => removeToast(id), type === 'error' ? 5000 : 3000)
    timersRef.current.set(id, timer)
  }, [removeToast])

  return (
    <ToastContext.Provider value={{ toast }}>
      {children}
      {/* 2026-08-17: 加上 aria-live + role (B 报告 P1-4) — 让读屏用户能听到所有 toast */}
      <div
        className="fixed top-6 left-1/2 -translate-x-1/2 z-[100] flex flex-col items-center gap-2.5 pointer-events-none"
        role="region"
        aria-live="polite"
        aria-label="通知消息"
      >
        {toasts.map(t => (
          <ToastItem key={t.id} toast={t} onClose={() => removeToast(t.id)} />
        ))}
      </div>
    </ToastContext.Provider>
  )
}

const ICONS: Record<ToastType, React.ReactNode> = {
  // 2026-08-17: 4xx 级色换 6xx 级 (B 报告 P1-4 + B P1-3 对比度同步)
  success: <CheckCircle2 className="w-4 h-4 text-emerald-600 flex-shrink-0" />,
  error: <AlertCircle className="w-4 h-4 text-red-600 flex-shrink-0" />,
  info: <Info className="w-4 h-4 text-primary flex-shrink-0" />,
}

const BG: Record<ToastType, string> = {
  success: 'border-emerald-500/20',
  error: 'border-red-500/20',
  info: 'border-primary/20',
}

function ToastItem({ toast, onClose }: { toast: Toast; onClose: () => void }) {
  const [visible, setVisible] = useState(false)

  useEffect(() => {
    requestAnimationFrame(() => setVisible(true))
  }, [])

  return (
    // 2026-08-17: 单一 toast 也加 role + aria-label (B 报告 P1-4 — error toast 用 assertive, 其他用 polite)
    <div
      role={toast.type === 'error' ? 'alert' : 'status'}
      aria-live={toast.type === 'error' ? 'assertive' : 'polite'}
      aria-label={toast.type === 'error' ? '错误通知' : toast.type === 'success' ? '成功通知' : '提示通知'}
      className={`pointer-events-auto flex items-center gap-3 px-4 py-3 rounded-xl border bg-card shadow-[0_8px_30px_rgba(0,0,0,0.08)] backdrop-blur-sm transition-all duration-300 max-w-sm ${BG[toast.type]} ${visible ? 'opacity-100 translate-y-0' : 'opacity-0 -translate-y-2'}`}
    >
      {ICONS[toast.type]}
      <span className="text-[13px] text-foreground flex-1">{toast.message}</span>
      <button onClick={onClose} className="text-muted-foreground hover:text-foreground transition-colors flex-shrink-0" aria-label="关闭通知">
        <X className="w-3.5 h-3.5" />
      </button>
    </div>
  )
}
