import React from 'react'
import ReactDOM from 'react-dom/client'
import { BrowserRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import App from './App'
import { ToastProvider } from '@panwatch/base-ui/components/ui/toast'
import { registerQueryClient } from './hooks/useApiQuery'
import { installGlobalErrorReporting } from './lib/error-report'
import './index.css'

installGlobalErrorReporting()

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
      <BrowserRouter>
        <ToastProvider>
          <App />
        </ToastProvider>
      </BrowserRouter>
    </QueryClientProvider>
  </React.StrictMode>
)
