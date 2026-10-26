import ts from '../../frontend/node_modules/typescript/lib/typescript.js'

// 只识别内容卡片；表单、页签栏和浮层有独立的边界用途。
export function cardSurfaces(source) {
  const ast = ts.createSourceFile('surface.tsx', source, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX)
  const surfaces = []
  const visit = node => {
    if (ts.isJsxOpeningElement(node) || ts.isJsxSelfClosingElement(node)) {
      const tag = node.tagName.getText(ast)
      const attributes = node.attributes.properties
      const attr = attributes.find(item => ts.isJsxAttribute(item) && item.name.getText(ast) === 'className')
      if (attr?.initializer) {
        const raw = attr.initializer.getText(ast)
        const named = tag === 'Card' || /(?:^|[\s"'`])(?:card|card-hover|card-subtle|card-surface|analysis-section)(?=[\s"'`]|$)/.test(raw)
        const control = attributes.some(item => ts.isJsxAttribute(item) && ['role', 'onDrop', 'onDragOver'].includes(item.name.getText(ast)))
        const floating = /\b(?:fixed|absolute|shadow-float|animate-spin|rounded-full)\b/.test(raw)
        const legacy = /^(div|section|article|aside|details)$/.test(tag) && !control && !floating
          && /\brounded(?:-[\w\[\]-]+)?/.test(raw)
          && /(?:[\s"'`])border(?:-\d+)?(?=[\s"'`]|$)/.test(raw)
        if (named || legacy) surfaces.push({ start: attr.initializer.getStart(ast), end: attr.initializer.end, raw, legacy, tag })
      }
    }
    ts.forEachChild(node, visit)
  }
  visit(ast)
  return surfaces
}

// 保留 focus-visible 的可访问性焦点环；移除普通/hover/选中态的装饰边线。
export const cardBorderClasses = /(?<![\w:-])(?:(?!focus(?:-visible|-within)?:)[\w\[\]=-]+:)*(?:border(?!-(?:0|none)\b)(?:-[\w./\[\]#%-]+)?|ring(?!-offset\b)(?:-[\w./\[\]#%-]+)?)(?=[\s"'`}]|$)/g
