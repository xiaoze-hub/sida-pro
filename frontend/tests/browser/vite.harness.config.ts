/**
 * 浏览器 fixture 专用 Vite 配置(root = frontend, 只为 harness 页面服务)。
 *
 * 存在的理由: `lightweight-charts` 是 `packages/biz-ui` 的依赖, 不在根 node_modules 里,
 * 而 harness 页面(位于 tests/browser/harness)要直接 import 它做对照实验。
 * 用 alias 指到工作区实际安装位置, 不去改动根 node_modules(共享目录, 不写)。
 */
import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import path from 'path'

export default defineConfig({
  root: path.resolve(__dirname, '../..'),
  plugins: [react()],
  resolve: {
    alias: {
      'lightweight-charts': path.resolve(
        __dirname,
        '../../packages/biz-ui/node_modules/lightweight-charts',
      ),
      '@': path.resolve(__dirname, '../../src'),
      '@sida/api': path.resolve(__dirname, '../../packages/api/src'),
      '@sida/base-ui': path.resolve(__dirname, '../../packages/base-ui/src'),
      '@sida/biz-ui': path.resolve(__dirname, '../../packages/biz-ui/src'),
    },
  },
  server: {
    host: '127.0.0.1',
    port: 5199,
    strictPort: true,
    // fixture 完全离线: 任何 /api 请求都不许出本机(harness 内部已用 fetch 桩拦掉)
    proxy: {},
  },
})
