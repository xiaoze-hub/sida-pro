/**
 * 全局测试配置(2026-09-18)。
 *
 * 背景: 今天两次"假红"都是同一类 —— 重渲染页(`board-heatmap` / `news-tab`)在机器有负载时
 * 首个渲染帧超过 testing-library 默认的 **1s** 等待上限(实测 1062ms / 5382ms), 于是断言超时失败,
 * 而代码没问题。CI runner 同样共享且繁忙, 这类抖动会变成噪声红。
 *
 * 处理: 只把**等待上限**放到 5s —— 断言的语义一个字没改(该等不到还是等不到, 只是给慢机器留余量)。
 * 只在有 DOM 的环境里生效(node 环境的纯逻辑测试不受影响); 用动态 import 避免 node 环境下
 * 因为加载 react-dom 而报错。
 */
if (typeof document !== 'undefined') {
  const { configure } = await import('@testing-library/react')
  configure({ asyncUtilTimeout: 5000 })
}

export {}
