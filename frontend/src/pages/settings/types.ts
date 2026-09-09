export interface Setting {
  key: string
  value: string
  description: string
}

export interface KeyDataSource {
  id: number
  name: string
  type: string
  provider: string
  enabled: boolean
  key_count?: number
}

export interface TemplatePayload {
  version: number
  exported_at?: string
  settings?: Record<string, string>
  agents?: any[]
  stocks?: any[]
}

export interface FeedbackStats {
  range_days: number
  total: number
  useful: number
  useless: number
  useful_rate: number
  by_day: Array<{ day: string; total: number; useful: number; useless: number; useful_rate: number }>
  by_agent: Array<{ agent_name: string; total: number; useful: number; useless: number; useful_rate: number }>
}

export interface AgentsHealth {
  timezone: string
  summary: {
    next_24h_count: number
    recent_failed_count: number
  }
}

export interface ServiceForm {
  name: string
  base_url: string
  api_key: string
}

export interface ModelForm {
  name: string
  service_id: number | null
  model: string
  capabilities: string[]
}

// BYOK(我的服务商): 用户自定义 LLM 服务商(2026-08-15)
export interface MyModelItem {
  name: string
  model: string
  is_default: boolean
  scene: string
  capabilities: string[]
}
export interface MyAIService {
  id: number
  name: string
  base_url: string
  api_key: string
  models: MyModelItem[]
  created_at?: string | null
}
export interface MyServiceForm {
  name: string
  base_url: string
  api_key: string
  model_name: string
  model: string
  is_default: boolean
  scene: string
  capabilities: string[]
}

export interface ChannelForm {
  name: string
  type: string
  config: Record<string, string>
}

export interface ChannelFieldDef {
  key: string
  label: string
  placeholder: string
  secret?: boolean
  required?: boolean
}

export const CHANNEL_TYPE_FIELDS: Record<string, { label: string; fields: ChannelFieldDef[]; hint?: string }> = {
  telegram: {
    label: 'Telegram',
    fields: [
      { key: 'bot_token', label: 'Bot Token', placeholder: '123456:ABC-DEF...', secret: true, required: true },
      { key: 'chat_id', label: 'Chat ID', placeholder: '-100123456789', required: true },
      { key: 'proxy', label: '代理', placeholder: 'http://192.168.1.1:7890 或 socks5://...' },
    ],
  },
  bark: {
    label: 'Bark',
    fields: [
      { key: 'device_key', label: 'Device Key', placeholder: '你的 Bark Device Key', required: true },
      { key: 'server_url', label: '服务器地址', placeholder: '默认 api.day.app，自建可填' },
    ],
  },
  dingtalk: {
    label: '钉钉机器人',
    fields: [
      { key: 'token', label: 'Webhook Token', placeholder: 'access_token 值', secret: true, required: true },
      { key: 'secret', label: '加签密钥', placeholder: 'SEC... (选填)', secret: true },
      { key: 'phones', label: '@手机号', placeholder: '逗号分隔，如 13800138000,13900139000' },
      { key: 'keyword', label: '关键字', placeholder: '若群机器人启用“关键字”，填入以自动附加' },
    ],
  },
  wecom: {
    label: '企业微信机器人',
    fields: [
      { key: 'webhook_key', label: 'Webhook Key', placeholder: 'Webhook URL 中 key= 后的值', secret: true, required: true },
    ],
  },
  lark: {
    label: '飞书机器人',
    fields: [
      { key: 'webhook_token', label: 'Webhook Token', placeholder: 'hook/ 后面的 token', secret: true, required: true },
    ],
  },
  serverchan: {
    label: 'Server酱',
    fields: [
      { key: 'sendkey', label: 'SendKey', placeholder: 'SCT...', secret: true, required: true },
    ],
  },
  pushplus: {
    label: 'PushPlus',
    fields: [
      { key: 'token', label: 'Token', placeholder: '你的 PushPlus Token', secret: true, required: true },
      { key: 'topic', label: '群组编码', placeholder: '选填，群组推送时填写' },
    ],
  },
  wechat_ilink: {
    label: '个人微信(iLink 直连)',
    fields: [],
    hint: '个人微信直通(腾讯官方 iLink 通道)。前往「设置 → 个人微信」扫码绑定即可，无需手填。',
  },
  discord: {
    label: 'Discord',
    fields: [
      { key: 'webhook_id', label: 'Webhook ID', placeholder: 'Webhook URL 中的 ID', required: true },
      { key: 'webhook_token', label: 'Webhook Token', placeholder: 'Webhook URL 中的 Token', secret: true, required: true },
    ],
  },
  pushover: {
    label: 'Pushover',
    fields: [
      { key: 'user_key', label: 'User Key', placeholder: '用户 Key', required: true },
      { key: 'app_token', label: 'App Token', placeholder: '应用 Token', secret: true, required: true },
    ],
  },
}

export const emptyServiceForm: ServiceForm = { name: '', base_url: '', api_key: '' }
export const emptyMyServiceForm: MyServiceForm = {
  name: '', base_url: '', api_key: '',
  model_name: '', model: '', is_default: true, scene: 'chat', capabilities: [],
}
export const emptyModelForm: ModelForm = { name: '', service_id: null, model: '', capabilities: [] }
export const emptyChannelForm: ChannelForm = { name: '', type: 'telegram', config: {} }

// 模型功能标签(capabilities): 彩色小徽标展示; key 与后端 capabilities 取值一致

// 模型功能标签徽标(缺失/未知标签按空处理,兼容后端未部署 capabilities)

// 敏感设置 key:值不回显(后端已掩码为 ********),输入框用密码态,掩码值不参与编辑
export const SECRET_SETTING_KEYS = new Set(['wudao_mcp_token', 'zhitu_token', 'tdx_api_key', 'ths_sdk_password'])
export const SECRET_MASK = '********'

// 数据源接口 Key 元信息(与后端 SETTING_DESCRIPTIONS 对齐)
export const DATA_SOURCE_KEYS: Array<{ key: string; name: string; desc: string }> = [
  { key: 'wudao_mcp_token', name: '悟道', desc: '竞价 / 题材数据源' },
  { key: 'zhitu_token', name: '智兔', desc: '分红 / 股东数据源 · 200次/天' },
  { key: 'tdx_api_key', name: '通达信', desc: '问小达 MCP · 自然语言投研 / 选股' },
]

export const STOCK_LINK_OPTIONS: Record<string, string> = { xueqiu: '雪球' }

// 2026-08-17: 全局搜索 — 各 section 可被搜的关键词(闭环修正 P0-1:用户搜'openai'应命中 AI 区块)
export const sectionSearchHints: Record<string, string[]> = {
  'sec-ai': ['AI', '服务商', '模型', 'openai', 'deepseek', 'chatgpt', 'claude', 'gpt'],
  'sec-notify': ['通知', '渠道', '微信', 'pushplus', 'server酱', 'webhook', '邮箱'],
  'sec-my-services': ['BYOK', '我的服务商', '自配', 'API Key'],
  'sec-subscriptions': ['订阅', '报告订阅', '定时', 'cron'],
  'sec-users': ['用户', '多用户', '权限', '管理员', 'owner', 'member'],
  'sec-keys': ['接口Key', '凭证', 'api key', 'token', 'key'],
  'sec-ths': ['同花顺', 'ths', '扫码', '登录'],
  'sec-system': ['系统', '偏好', '主题', '深色', '密度'],
  'sec-pack': ['配置包', '导入', '导出', '模板', '备份'],
  'sec-feedback': ['反馈', '有用', '没用'],
  'sec-llm-usage': ['LLM', '用量', 'token', '费用', 'API 用量'],
}
