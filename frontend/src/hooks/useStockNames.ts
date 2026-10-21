import { useEffect, useSyncExternalStore } from 'react'

import {
  fetchNames,
  getName,
  getStockNamesVersion,
  subscribeStockNames,
} from '@/lib/stockNames'

/**
 * 名称映射 hook(2026-10-21) —— 把「6 位代码」补成「名称 + 代码」。
 *
 * 用法:
 * ```tsx
 * const names = useStockNames(['600111', '000001'])
 * names['600111'] // => '北方稀土' | undefined
 * ```
 *
 * 约定:
 *  - **render 阶段不发请求**: 只同步读 `stockNames` 的模块级缓存; 取数一律在 effect 里触发;
 *  - codes 变了(去重排序后的字符串变了)才重拉 —— 数组字面量每次渲染都是新引用,
 *    直接进依赖会无限重取;
 *  - 返回值里**没有键 = 不知道**(不是空字符串), 展示层据此回退成只显示代码;
 *  - 端点未上线/请求失败 ⇒ 映射为空, 页面照常渲染, 不抛错、不弹错。
 */
export function useStockNames(codes: string[]): Record<string, string> {
  // 排序去重后的代码串当依赖 key(与 useBatchCloses 同口径)
  const key = Array.from(
    new Set(
      (codes ?? [])
        .map((c) => String(c ?? '').trim().toUpperCase())
        .filter(Boolean),
    ),
  )
    .sort()
    .join(',')

  // 缓存是模块级可变状态: 用外部 store 订阅 —— 名称到达时自动重渲染, 不在 render 里改 state。
  const version = useSyncExternalStore(subscribeStockNames, getStockNamesVersion, getStockNamesVersion)

  useEffect(() => {
    if (!key) return
    // 内部已做缓存/去重/分批/串行, 且永不 reject(失败静默)
    void fetchNames(key.split(','))
  }, [key])

  const names: Record<string, string> = {}
  if (key) {
    for (const code of key.split(',')) {
      const name = getName(code)
      if (name) names[code] = name
    }
  }
  // version 依赖: 缓存内容变化 ⇒ 本组件已重渲染, 这里重新读一遍即可(map 本身不可观察)
  void version
  return names
}

export default useStockNames
