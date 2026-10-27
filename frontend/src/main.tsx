import React from 'react'
import ReactDOM from 'react-dom/client'
import { BrowserRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import App from './App'
import { ToastProvider } from '@sida/base-ui/components/ui/toast'
import { I18nProvider } from './hooks/useI18n'
import { registerQueryClient } from './hooks/useApiQuery'
import { installGlobalErrorReporting } from './lib/error-report'
import { migrateLegacyStorage } from './lib/storage-migrate'
import './index.css'

installGlobalErrorReporting()
// 品牌改名 panwatch_* → sida_*：必须在任何组件读 localStorage 之前跑一次，
// 否则老用户的主题/自选/引导状态会被当成"没设置过"。
migrateLegacyStorage()

// W3.7/D7: 服务端状态统一交给 TanStack Query(staleTime 与行情刷新周期对齐 30s;
// 交易场景关掉窗口聚焦重取, 避免切回页面时的请求风暴)。
const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      staleTime: 30_000,
      retry: 1,
      refetchOnWindowFocus: false,
    },
  },
})

registerQueryClient(queryClient)

ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <QueryClientProvider client={queryClient}>
      <I18nProvider>
        <BrowserRouter>
          <ToastProvider>
            <App />
          </ToastProvider>
        </BrowserRouter>
      </I18nProvider>
    </QueryClientProvider>
  </React.StrictMode>
)
