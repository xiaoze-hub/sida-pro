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
      '@sida/api': path.resolve(__dirname, './packages/api/src'),
      '@sida/base-ui': path.resolve(__dirname, './packages/base-ui/src'),
      '@sida/biz-ui': path.resolve(__dirname, './packages/biz-ui/src'),
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
  build: {
    chunkSizeWarningLimit: 1200,
    // 2026-10-27（vite 8 升级）：vite 8 默认用 lightningcss 压 CSS，
    // 本项目现有样式里有它不接受的选择器写法（`Unexpected token Delim('.')`），
    // 构建直接失败。先固定回 esbuild 压缩（vite 5 时代的默认），
    // 让依赖升级能落地；迁移到 lightningcss 单独排期（需要逐页截图比对）。
    cssMinify: 'esbuild',
    rollupOptions: {
      output: {
        manualChunks(id) {
          if (id.includes('node_modules')) {
            if (id.includes('echarts') || id.includes('zrender')) {
              return 'vendor-echarts'
            }
            if (id.includes('lightweight-charts')) {
              return 'vendor-charts'
            }
            if (id.includes('react') || id.includes('react-dom')) {
              return 'vendor-react'
            }
          }
        },
      },
    },
  },
})
