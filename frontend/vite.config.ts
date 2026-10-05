import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import path from 'path'

export default defineConfig({
  // 测试: 只放宽**等待上限**(见 tests/setup.ts), 不动断言语义; environment 仍由各文件 docblock 决定。
  test: {
    setupFiles: ['tests/setup.ts'],
    testTimeout: 20000,
    hookTimeout: 20000,
    /**
     * B1 首屏冷启动回归(2026-10-05): `kline-load-chain.test.tsx` 渲染**真** KlineChart, 只把
     * canvas 层 `lightweight-charts` 换成无副作用的假实现(见 `tests/stubs/lightweight-charts.ts`)。
     * 该包是 `packages/biz-ui` 的依赖(不在根 node_modules, 测试文件 import 不到 / `vi.mock` 也拦不住),
     * 故在测试期做别名替换。只在**测试**生效(dev/build 不受影响); 未引用该包的用例零影响。
     */
    alias: {
      'lightweight-charts': path.resolve(__dirname, './tests/stubs/lightweight-charts.ts'),
    },
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
