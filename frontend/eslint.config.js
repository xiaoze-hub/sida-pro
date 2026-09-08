import tseslint from 'typescript-eslint'
import reactHooks from 'eslint-plugin-react-hooks'

// E3(2026-09-09) 前端门禁: no-unused-vars 与 react-hooks 两条铁律为 error。
// 刻意不整包开启 recommended —— 存量 any/类型告警走 tsc -b 把关, 这里只管
// 会直接产运行时坑的死代码与 hook 依赖缺失。
export default tseslint.config(
  { ignores: ['dist', '**/dist/**', 'node_modules', 'public'] },
  {
    files: ['src/**/*.{ts,tsx}', 'packages/*/src/**/*.{ts,tsx}'],
    plugins: {
      '@typescript-eslint': tseslint.plugin,
      'react-hooks': reactHooks,
    },
    languageOptions: {
      parser: tseslint.parser,
      parserOptions: { sourceType: 'module', ecmaVersion: 'latest', ecmaFeatures: { jsx: true } },
    },
    rules: {
      '@typescript-eslint/no-unused-vars': ['error', { argsIgnorePattern: '^_', varsIgnorePattern: '^_' }],
      'react-hooks/rules-of-hooks': 'error',
      'react-hooks/exhaustive-deps': 'error',
    },
  },
)
