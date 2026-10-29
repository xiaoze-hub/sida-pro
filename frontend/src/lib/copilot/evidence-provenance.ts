import type { CopilotRunStep } from '@/lib/copilot/api'
import type { ReadinessState } from '@/lib/copilot/evidence-readiness'

/**
 * 回答证据可追溯（2026-10-29 P1）—— **只描述真实存在的结构证据**。
 *
 * 为什么这个模块这么「吝啬」：这一层的价值全在「说得出凭据」，一旦编一条就等于
 * 给用户一个假的核验结论。硬事实（基线 1537a081 核对源码）：
 *
 *  1. `chat_messages`（`src/db/models.py:1384`）只有 id / conversation_id / role /
 *     content / created_at —— **没有 run_id**。SSE `done` 帧（`src/web/api/chat.py:2284`）
 *     同样只回 `{id, role, content, created_at}`。
 *     ⇒ 「这条回答 ↔ 哪一轮 run」目前**只能按提问文本匹配**（`lib/copilot/match-run.ts`），
 *       那是推断。`association` 因此分成 `run-id`（服务端给的）与 `question-text`（推断）。
 *       推断来的关联永远 `verified: false`，UI 必须把「未验证」写在脸上。
 *
 *  2. 依据（逐句）与工具结果之间**没有任何 ID / 序号关联**：`CopilotRunStep`
 *     只有 seq / tool / ok / elapsed_ms / args / result_digest，回答正文从不引用 step。
 *     ⇒ `rowLevel` 恒为 `false`。绝不许按正则/关键词把某句话「对到」某个工具 ——
 *       那正是最容易伪造的来源。逐句出处只有在后端把「依据行 → step」落库后才可能成立。
 *
 *  3. 真正可核验的逐条取数痕迹是 `stage` 流折出来的齐套度（`evidence-readiness.ts`）：
 *     它属于**本条消息自己的流**，所以能说「本轮取到/没取到哪几路」。但它说明的是
 *     「哪几路取了」，**不是**「哪一句来自哪一路」。两层不许混。
 *
 * 三条纪律：
 *  - 没有 run 就没有 run_id：不补、不猜、不写占位的 0。
 *  - 步骤没加载 → `stepCount: null`，绝不拿 `run.tool_calls`（自报汇总值）冒充已加载明细。
 *  - 缺口数字只在真的折过 stage 事件时存在；没有取数痕迹就是 `null`，不是 0。
 */

/** 回答 ↔ run 的关联方式。只有 `run-id` 是服务端给的关联。 */
export type RunAssociation = 'run-id' | 'question-text' | 'none'

/** 溯源需要的 run 字段（故意只收这几个：都是 `/copilot/runs` 真返的）。 */
export interface TraceRun {
  run_id: string
  created_at?: string | null
  status?: string | null
  tool_calls?: number | null
  elapsed_ms?: number | null
  question?: string | null
}

export interface AnswerTrace {
  /** 关联是否可核验：仅当 association === 'run-id' 且确实有 run */
  verified: boolean
  association: RunAssociation
  runId: string | null
  createdAt: string | null
  status: string | null
  /** 已加载的真实步骤条数；步骤未加载 → null（不是 0，也不是 run.tool_calls） */
  stepCount: number | null
  /** 已加载步骤里真实出现过的工具名（按 seq 保序去重） */
  tools: string[]
  /** 本轮取数「未取到」条数；没有取数痕迹 → null */
  failedCount: number | null
  /** 本轮取数「未确认」条数；没有取数痕迹 → null */
  unknownCount: number | null
  /** 逐句/逐条依据是否可指到来源。当前链路恒为 false，见文件头第 2 条。 */
  rowLevel: false
}

export interface AnswerTraceInput {
  run?: TraceRun | null
  /** 已加载的步骤明细；`undefined`/`null` = 还没加载（区别于 `[]` = 加载了但零步） */
  steps?: CopilotRunStep[] | null
  /** 这个 run 是怎么对到这条回答的；缺省按最保守的推断值处理 */
  association?: RunAssociation
  readiness?: ReadinessState | null
}

/** 保守默认：调用方没说清关联方式时，按「文本推断」对待（绝不默认已核验）。 */
const DEFAULT_ASSOCIATION: RunAssociation = 'question-text'

export function buildAnswerTrace(input: AnswerTraceInput): AnswerTrace {
  const run = input.run ?? null
  // 没有 run 就没有关联可谈：哪怕调用方传了 run-id 也降级（无凭证不得自称已核验）
  const association: RunAssociation = run ? (input.association ?? DEFAULT_ASSOCIATION) : 'none'

  const steps = input.steps ?? null
  const tools: string[] = []
  if (steps) {
    for (const s of steps) {
      const name = (s?.tool || '').trim()
      if (name && !tools.includes(name)) tools.push(name)
    }
  }

  const readiness = input.readiness ?? null
  const hasReadiness = Boolean(readiness && readiness.items.length)

  return {
    verified: association === 'run-id',
    association,
    runId: run?.run_id ?? null,
    createdAt: run?.created_at ?? null,
    status: run?.status ?? null,
    stepCount: steps ? steps.length : null,
    tools,
    failedCount: hasReadiness ? (readiness as ReadinessState).failed : null,
    unknownCount: hasReadiness ? (readiness as ReadinessState).unknown : null,
    rowLevel: false,
  }
}

/** 关联方式人话。`question-text` 必须带「未验证」——它可能对错轮。 */
export function associationLabel(a: RunAssociation): string {
  if (a === 'run-id') return '按 run_id 关联'
  if (a === 'question-text') return '按提问文本匹配（未验证）'
  return '未关联到运行记录'
}
