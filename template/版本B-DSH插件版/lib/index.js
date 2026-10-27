/**
 * dsh-memory-core —— 把本地记忆内核接进 DSH
 *
 * 设计边界（和 memory-core 一致）：
 *   · 只注册工具，不自动把记忆塞进 system prompt / 对话
 *   · 宿主（会话框、QQ 插件、桥）自己决定什么时候调
 *   · 本插件不存数据：所有数据在 memory-core 的 SQLite 里
 *
 * 安装：dsh plugin --profile <p> add link:<本目录>
 * 依赖：memory-core 以 HTTP 跑在本机（默认 http://127.0.0.1:8095）
 */

export const name = 'dsh-memory-core'
export const VERSION = '0.1.0'

// 工具 + 会话服务（agent/created 用于挂 systemPrompt 段）
export const inject = ['tools', 'sessionController']

const DEFAULT_BASE = 'http://127.0.0.1:8095'

function baseOf(config = {}) {
  return String(config.coreUrl || process.env.MEMORY_CORE_URL || DEFAULT_BASE).replace(/\/+$/, '')
}

function personOf(config = {}, args = {}) {
  return String(args.person || config.personId || process.env.MEMORY_PERSON || '')
}

async function post(base, path, body, timeoutMs = 8000) {
  const ctrl = new AbortController()
  const timer = setTimeout(() => ctrl.abort(), timeoutMs)
  try {
    const res = await fetch(base + path, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body || {}),
      signal: ctrl.signal,
    })
    if (!res.ok) throw new Error('HTTP ' + res.status)
    return await res.json()
  } finally {
    clearTimeout(timer)
  }
}

function failText(e) {
  return '记忆内核连不上（' + String(e?.message || e).slice(0, 120) + '），这次不编。'
}

function renderRecall(data) {
  const rows = data?.results || data?.items || []
  if (!rows.length) return '没翻到。'
  return rows
    .map((r, i) => {
      const score = r.score_norm ?? r.score ?? ''
      const w = r.weight ?? r.decay ?? ''
      const head = `[${i + 1}] ${r.title || ''}`
      const meta = [score !== '' ? `相关 ${score}` : '', w !== '' ? `权重 ${w}` : ''].filter(Boolean).join(' · ')
      return head + (meta ? `（${meta}）` : '') + '\n' + String(r.text || '').slice(0, 200)
    })
    .join('\n')
}

const outSchema = {
  type: 'object',
  properties: { ok: { type: 'boolean' }, text: { type: 'string' } },
  required: ['ok', 'text'],
}

const render = (_a, out) => [{ type: 'text', text: out.text }]

const injectCache = { text: '' }

async function refreshBundle(base, config) {
  try {
    const r = await post(base, '/context', {
      q: '',
      include_self: true,
      diary_days: Number(config.diaryDays || 0),
      min_score: Number(config.memoryMinScore ?? 0.5),
      max_chars: Number(config.injectMaxChars || 1600),
    })
    if (r && typeof r.text === 'string') injectCache.text = r.text
  } catch { /* 内核不可达就保持上一版 */ }
}

function wantAgent(agent, config) {
  if (config.autoInject === false) return false
  const cwd = String(agent?.session?.header?.cwd || agent?.header?.cwd || '')
  const want = String(config.injectCwd || '')
  return !want || cwd.toLowerCase().includes(want.toLowerCase())
}

export function apply(ctx, config = {}) {
  const base = baseOf(config)
  const disposers = []
  let timer = null

  // 自动注入：每 30 秒刷新一次注入包；text() 同步返缓存
  if (config.autoInject !== false) {
    refreshBundle(base, config)
    timer = setInterval(() => refreshBundle(base, config), 30000)
    if (timer.unref) timer.unref()
    const offAgent = ctx.on?.('agent/created', ({ agent } = {}) => {
      if (!wantAgent(agent, config)) return
      try {
        agent?.ctx?.systemPrompt?.section({
          name: 'memory:inject',
          order: 8400,
          text: () => injectCache.text,
        })
      } catch (e) {
        console.warn('[dsh-memory-core] 注入段没挂上', e?.message || e)
      }
    })
    if (typeof offAgent === 'function') disposers.push(offAgent)
  }

  const tools = [
    {
      name: 'memory_recall',
      description: '从长期记忆里找相关的事。先翻再说话；没翻到就说没翻到，不要编。',
      parameters: {
        type: 'object',
        properties: {
          query: { type: 'string', description: '要找什么，几个词就够' },
          top: { type: 'number', description: '最多几条，默认 5' },
          person: { type: 'string', description: '按人物过滤（可选）' },
        },
        required: ['query'],
      },
      output: { schema: outSchema, render },
      async execute(args = {}) {
        try {
          const data = await post(base, '/recall', {
            q: args.query || '',
            top: Number(args.top || 5),
            person: personOf(config, args),
            rerank: 'off',
          })
          return { ok: true, text: renderRecall(data) }
        } catch (e) {
          return { ok: false, text: failText(e) }
        }
      },
    },
    {
      name: 'memory_remember',
      description: '把值得长期记住的事写进记忆。纯事实，不要写推测。',
      parameters: {
        type: 'object',
        properties: {
          content: { type: 'string', description: '要记住的事' },
          title: { type: 'string', description: '短标题（可选）' },
          person: { type: 'string', description: '关于谁（可选）' },
          importance: { type: 'number', description: '0~1，默认按类型给' },
          tags: { type: 'array', items: { type: 'string' }, description: '标签（可选）' },
        },
        required: ['content'],
      },
      output: { schema: outSchema, render },
      async execute(args = {}) {
        try {
          const data = await post(base, '/remember', {
            title: args.title || '',
            content: args.content || '',
            person_id: personOf(config, args),
            tags: args.tags || [],
            importance: args.importance,
          })
          const id = data?.id || ''
          if (data?.mode && data.mode !== 'local') {
            return { ok: true, text: '已写入（转发模式）。' + (id ? ' id=' + id : '') }
          }
          return { ok: true, text: '已记住。' + (id ? ' id=' + id : '') }
        } catch (e) {
          return { ok: false, text: failText(e) }
        }
      },
    },
    {
      name: 'memory_context',
      description: '生成一段可放进上下文的记忆文本。只生成，不自动注入；用不用由调用方决定。',
      parameters: {
        type: 'object',
        properties: {
          query: { type: 'string', description: '相关主题（可选）' },
          person: { type: 'string', description: '按人物过滤（可选）' },
          max_chars: { type: 'number', description: '字符预算，默认 1200' },
        },
        required: [],
      },
      output: { schema: outSchema, render },
      async execute(args = {}) {
        try {
          const data = await post(base, '/context', {
            q: args.query || '',
            person: personOf(config, args),
            max_chars: Number(args.max_chars || 1200),
          })
          return { ok: true, text: data?.text || '没有可用的记忆。' }
        } catch (e) {
          return { ok: false, text: failText(e) }
        }
      },
    },
    {
      name: 'memory_log',
      description: '把这一轮对话记进日志层（不判断、不抽取）。跨 30 分钟会自动结算成会话事件。',
      parameters: {
        type: 'object',
        properties: {
          user: { type: 'string', description: '用户说的话' },
          reply: { type: 'string', description: '这边的回复（可选）' },
          person: { type: 'string', description: '谁（可选）' },
        },
        required: ['user'],
      },
      output: { schema: outSchema, render },
      async execute(args = {}) {
        try {
          const data = await post(base, '/log', {
            user: args.user || '',
            reply: args.reply || '',
            person: personOf(config, args),
          })
          const closed = data?.closed
          const extra = closed
            ? `；上一段会话已结算 ${Array.isArray(closed.event_ids) ? closed.event_ids.length : 1} 条事件`
            : ''
          return { ok: true, text: '已记日志' + extra + '。' }
        } catch (e) {
          return { ok: false, text: failText(e) }
        }
      },
    },
    {
      name: 'memory_sweep',
      description: '结算所有空闲超过 30 分钟的会话（把最后一段对话落成事件）。',
      parameters: { type: 'object', properties: {}, required: [] },
      output: { schema: outSchema, render },
      async execute() {
        try {
          const data = await post(base, '/sweep', {})
          const n = Array.isArray(data?.closed) ? data.closed.length : 0
          return { ok: true, text: n ? `结算了 ${n} 段会话。` : '没有需要结算的会话。' }
        } catch (e) {
          return { ok: false, text: failText(e) }
        }
      },
    },
  ]

  if (!ctx?.tools?.register) {
    console.warn('[dsh-memory-core] 宿主没有 tools 服务，工具未注册')
    return { dispose() {} }
  }

  for (const spec of tools) {
    try {
      const d = ctx.tools.register(spec)
      if (typeof d === 'function') disposers.push(d)
      else if (d?.dispose) disposers.push(() => d.dispose())
    } catch (e) {
      console.warn('[dsh-memory-core] 工具注册失败', spec.name, e?.message || e)
    }
  }

  const dispose = () => {
    if (timer) clearInterval(timer)
    for (const d of disposers) {
      try { d() } catch { /* ignore */ }
    }
  }
  ctx.on?.('dispose', dispose)
  return { dispose }
}
