/**
 * 品牌标记(2026-09-12, 对应 App 图标 B 方案: 三根递增柱 + 上行箭头)。
 *
 * 与 `frontend/public/icon.svg` 同一造型, 但为 16~32px 的界面场景做了小尺寸简化:
 * 折线去掉锯齿走直线、柱体加粗留白, 否则缩小后糊成一团。单色(currentColor),
 * 用于侧边栏/移动端头部/登录页的渐变方章内。
 */
export function BrandMark({ className }: { className?: string }) {
  return (
    <svg viewBox="0 0 24 24" className={className} aria-hidden="true" focusable="false">
      <g fill="currentColor">
        <rect x="2.2" y="15.5" width="4.2" height="5.5" rx="2.1" />
        <rect x="8.9" y="13.5" width="4.2" height="7.5" rx="2.1" />
        <rect x="15.6" y="11.5" width="4.2" height="9.5" rx="2.1" />
      </g>
      <polyline
        points="3,13 17.4,5.9"
        fill="none"
        stroke="currentColor"
        strokeWidth="2.4"
        strokeLinecap="round"
      />
      <polygon points="21,4.2 18.7,8.7 16.1,3.1" fill="currentColor" />
    </svg>
  )
}
