/**
 * Aurabot 用户模块：把 memory-core 接进大肥鱼
 *
 * 用法：把整个文件夹复制到 %USERPROFILE%\.dsh\plugins-data\aurabot\modules\memory-core\
 * 保存后几秒内自动加载（面板「扩展」页能看到）。
 *
 * 依据 Aurabot 扩展接口 v1（src/modules/API.md）：
 *   api.on('message') / api.tool(({chat,level}) => ...) / api.rule(...)
 */

const DEFAULTS = {
  enabled: true,
  coreUrl: 'http://127.0.0.1:8095',
  autoLog: true,
  socialReadOnly: true,
}

function post(url, body, timeoutMs = 5000) {
  return fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body || {}),
    signal: AbortSignal.timeout(timeoutMs),
  })
}

export default {
  id: 'memory-core',
  name: '长期记忆（memory-core）',
  version: '0.1.0',
  api: 1,
  description: '把长期事实记忆接给她：日志自动记，召回/上下文/写入用工具。',
  defaults: { ...DEFAULTS },
  settings: [
    { key: 'enabled', label: '启用', type: 'switch' },
    { key: 'coreUrl', label: 'memory-core 地址', type: 'text', hint: '默认 http://127.0.0.1:8095' },
    { key: 'autoLog', label: '自动记日志', type: 'switch', hint: '只记录，不判断；跨 30 分钟由内核结算' },
    { key: 'socialReadOnly', label: '群聊/他人只读', type: 'switch', hint: '只给召回和上下文，不给写入' },
  ],

  setup(api) {
    const base = () => String(api.settings().coreUrl || DEFAULTS.coreUrl).replace(/\/+$/, '')
    const on = () => api.settings().enabled !== false

    // 入站：只追加日志，不参与她回不回
    api.on('message', (msg) => {
      if (!on() || api.settings().autoLog === false) return false
      if (!msg?.text) return false
      post(base() + '/log', { user: msg.text, person: 'qq:' + msg.userId })
        .catch((e) => api.log.warn('[memory-core] 记日志失败：' + (e?.message || e)))
      return false
    })

    const recallTool = api.defineTool({
      name: 'memory_recall',
      description: '翻长期记忆。想说“以前有一次”之前先翻，翻不到就说没翻到。',
      properties: {
        query: { type: 'string', description: '要找什么' },
        top: { type: 'number', description: '最多几条，默认 5' },
      },
      required: ['query'],
      async execute({ query, top }, ctx) {
        try {
          const r = await post(base() + '/recall', { q: query, top: Number(top || 5), rerank: 'off' })
          const d = await r.json()
          const rows = d.results || []
          return {
            ok: true,
            message: rows.length
              ? rows.map((x, i) => `[${i + 1}] ${x.title}\n${String(x.text || '').slice(0, 180)}`).join('\n')
              : '没翻到。',
          }
        } catch (e) {
          return { ok: false, message: '记忆内核连不上（' + (e?.message || e) + '），这次不编。' }
        }
      },
    })

    const contextTool = api.defineTool({
      name: 'memory_context',
      description: '把相关记忆整理成一段可放进上下文的文字（只生成，不自动注入）。',
      properties: {
        query: { type: 'string', description: '主题（可选）' },
        max_chars: { type: 'number', description: '字符预算，默认 1200' },
      },
      async execute({ query, max_chars }) {
        try {
          const r = await post(base() + '/context', { q: query || '', max_chars: Number(max_chars || 1200) })
          const d = await r.json()
          return { ok: true, message: d.text || '没有可用的记忆。' }
        } catch (e) {
          return { ok: false, message: '记忆内核连不上（' + (e?.message || e) + '）。' }
        }
      },
    })

    const rememberTool = api.defineTool({
      name: 'memory_remember',
      description: '把值得长期记住的事实写进记忆。纯事实，不写推测。',
      properties: {
        content: { type: 'string', description: '要记住的事' },
        title: { type: 'string', description: '短标题（可选）' },
        importance: { type: 'number', description: '0~1（可选）' },
      },
      required: ['content'],
      async execute({ content, title, importance }, ctx) {
        const chat = ctx?.chat
        const person = chat?.kind === 'private' ? 'qq:' + chat.userId : ''
        try {
          const r = await post(base() + '/remember', {
            title: title || '', content, person_id: person, importance,
          })
          const d = await r.json()
          return { ok: true, message: '已记住。' + (d.id ? ' id=' + d.id : '') }
        } catch (e) {
          return { ok: false, message: '记忆内核连不上（' + (e?.message || e) + '）。' }
        }
      },
    })

    // full（主人私聊）给三个；social 默认只给读
    api.tool(({ level }) => {
      if (!on()) return []
      if (level === 'full') return [recallTool, contextTool, rememberTool]
      if (api.settings().socialReadOnly !== false) return [recallTool, contextTool]
      return [recallTool, contextTool, rememberTool]
    })

    api.rule(() => '你有长期记忆工具：想提“以前”时先用 memory_recall 翻；需要长段落记忆时用 memory_context；重要事实用 memory_remember 记下。')
  },
}
