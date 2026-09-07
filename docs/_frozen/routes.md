# P0冻结-路由表(2026-09-07, main@7a4612d)
> 来源: frontend/src/App.tsx + pages/。6项主导航收敛目标: 驾驶舱/行情/机会/投研/我的/系统。

| 路由 | 页面 | 权限 |
|---|---|---|
| / | Dashboard驾驶舱 | 登录 |
| /opportunities | Opportunities机会 | view_opportunities |
| /dark-fund-top | DarkFundTop暗盘 | view_opportunities |
| /forecast, /quote(别名), /l2 | Quote行情+L2Orderbook | view_forecast |
| /index/:symbol | IndexDetail | 登录 |
| /boards/:blockCode | BoardDetail | 登录 |
| /portfolio (/stocks跳转此) | Stocks持仓 | edit_portfolio |
| /analysis/:symbol/:date | AnalysisDetail研报详情 | 登录 |
| /system (/agents,/datasources归并) | System系统 | owner/tab鉴权 |
| /reports (/history归并) | ReportsHub | owner/tab鉴权 |
| /shadow (/paper-trading归并) | ShadowHub | owner/tab鉴权 |
| /notifications (/alerts归并) | NotificationsHub | owner/tab鉴权 |
| /settings (/audit,/help归并) | SettingsHub | owner/tab鉴权 |
| /profile | Profile | 登录 |
| /login | Login | 公开 |

后退化: 68个API模块(见ai-tools表) vs 15个路由组, settings/system/reports三Hub臃肿, P3重点拆chart独立图层。
CRLF命中(8个, P3修): TabbedPage/PageTabs/ContextCard/NotificationsHub/ReportsHub/SettingsHub/System/ShadowHub。
