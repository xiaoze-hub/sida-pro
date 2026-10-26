// 三层滤网结论行 (ThreeTierVerdictRow) 前端组件契约。
import { describe, expect, it } from 'vitest'
import { readFileSync } from 'fs'
import { resolve } from 'path'

const fileSuggestTab = readFileSync(
  resolve(__dirname, '../../src/pages/workbench/tabs/SuggestTab.tsx'),
  'utf-8',
)

const fileVerdictRow = readFileSync(
  resolve(__dirname, '../../src/components/ThreeTierVerdictRow.tsx'),
  'utf-8',
)

describe('ThreeTierVerdictRow 嵌入契约', () => {
  it('SuggestTab 挂载并传导 symbol 与 market', () => {
    expect(fileSuggestTab).toMatch(/import \{ ThreeTierVerdictRow \} from '@\/components\/ThreeTierVerdictRow'/)
    expect(fileSuggestTab).toMatch(/<ThreeTierVerdictRow symbol=\{symbol\} market=\{market\} \/>/)
  })

  it('ThreeTierVerdictRow 串联三层端点 (/market/phase, /catalyst/calendar, /wyckoff)', () => {
    expect(fileVerdictRow).toMatch(/\/market\/phase/)
    expect(fileVerdictRow).toMatch(/\/catalyst\/calendar\?days=14/)
    expect(fileVerdictRow).toMatch(/\/klines\/\$\{encodeURIComponent\(symbol\)\}\/wyckoff\?market=/)
  })

  it('结论行文案诚实: 包含客观证据提示且不含单点买卖指令', () => {
    expect(fileVerdictRow).toMatch(/客观证据 · 不构成买卖指令/)
    expect(fileVerdictRow).not.toMatch(/买入信号|直接买入|立刻卖出|全力抄底/)
  })
})
