import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import path from 'path'

export default defineConfig({
  // 测试: 只放宽**等待上限**(见 tests/setup.ts), 不动断言语义; environment 仍由各文件 docblock 决定。
  test: {
    setupFiles: ['tests/setup.ts'],
    testTimeout: 20000,
    hookTimeout: 20000,
  },
  plugins: [react()],
  resolve: {
    alias: {
      '@': path.resolve(__dirname, './src'),
      '@panwatch/api': path.resolve(__dirname, './packages/api/src'),
      '@panwatch/base-ui': path.resolve(__dirname, './packages/base-ui/src'),
      '@panwatch/biz-ui': path.resolve(__dirname, './packages/biz-ui/src'),
    },
  },
  server: {
    host: '0.0.0.0',
    port: 5183,
    strictPort: true,
    proxy: {
      '/api': 'http://127.0.0.1:8000',
    },
  },
})
