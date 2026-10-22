import { fetchAPI } from './client'

/**
 * 情景矩阵(结构化关键位)—— 后端 `GET /api/klines/{symbol}/scenario`。
 *
 * 与 chat 工具 `get_scenario_matrix` 同一套计算(`src/core/scenario_engine.py::build_matrix`):
 * 只给**条件-反应**(触发/目标/应对/失效), 不预测方向。关键位不足时后端返回
 * `available=false` + `reason`(前端据此静默不画, 不编造关键位)。
 *
 * 字段说明:
 *  - `levels.resist` / `levels.support`: 结构性关键位(阻力/支撑);
 *  - `scenarios[].target`: 三档情景的目标位 —— 走强/震荡/走弱, 可能是数字,
 *    也可能是 "32.5" 或 "下方无明确支撑(需重新评估)" 这类字符串(前端只取能解析的数字)。
 */
export interface ScenarioLevels {
  price?: number | null
  resist?: number | null
  support?: number | null
  ma20?: number | null
  ma60?: number | null
  band_hi?: number | null
  band_lo?: number | null
  [k: string]: unknown
}

export interface ScenarioItem {
  /** 结构化档位标识: 'strong' | 'flat' | 'weak' */
  id?: string | null
  /** 既有形态的档位名: '走强' | '震荡' | '走弱' */
  name?: string | null
  label?: string | null
  trigger?: string | null
  /** 目标位: 数字, 或 "32.5" / "下方无明确支撑(需重新评估)" 这类字符串 */
  target?: number | string | null
  response?: string | null
  action?: string | null
  invalid?: string | null
}

export interface ScenarioMatrixResponse {
  available: boolean
  /** 关键位不足时的原因(available=false 时有值) */
  reason?: string | null
  price?: number | null
  as_of?: string | null
  levels?: ScenarioLevels | null
  scenarios?: ScenarioItem[] | null
}

/**
 * 取情景矩阵。**失败返回 null**(不抛错) —— 图层是增强, 取不到就不画,
 * 绝不让它把整页拖进错误态。
 */
export async function fetchScenario(
  symbol: string,
  market = 'CN',
): Promise<ScenarioMatrixResponse | null> {
  if (!symbol) return null
  try {
    const res = await fetchAPI<ScenarioMatrixResponse>(
      `/klines/${encodeURIComponent(symbol)}/scenario?market=${encodeURIComponent(market)}`,
    )
    return res ?? null
  } catch {
    // 显式降级: 取不到 = 本次不画情景关键位(不编造), 不弹错
    return null
  }
}
