/**
 * dsh-onebot-qq 子插件：把 memory-core 接进 QQ 会话
 *
 * 位置：复制到 <dsh-onebot-qq>/plugins/memory-bridge/ 下，重启 DSH。
 * 它用的是 onebot-qq 的子插件契约 v1（bot.on / bot.registerTool / bot.onDispose）。
 *
 * 能做的：
 *   · 每轮唤醒时把用户消息写进 memory-core 日志层（before-turn，fire-and-forget）
 *   · 注册三个工具：mcp__qq__memory_recall / memory_context / memory_remember
 * 不能做的：
 *   · 没有"一轮结束"钩子；出站只能 onOutbound 装饰，不含原始入站，所以不自动记回复
 */

const DEFAULTS = {
  enabled: true,
  coreUrl: 'http://127.0.0.1:8095',
  autoLog: true,
}

function post(url, body, timeoutMs = 5000) {
  const ctrl = new AbortController()
  const timer = setTimeout(() => ctrl.abort(), timeoutMs)
  return fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body || {}),
    signal: ctrl.signal,
  }).finally(() => clearTimeout(timer))
}

export default {
  id: 'memory-bridge',
  title: '长期记忆（memory-core）',
  order: 95,
  configSchema: {
    enabled: { type: 'bool', default: true, group: '长期记忆', label: '启用' },
    coreUrl: {
      type: 'str', default: DEFAULTS.coreUrl, group: '长期记忆',
      label: 'memory-core 地址', hint: '默认 http://127.0.0.1:8095，需先起 memory-core',
    },
    autoLog: { type: 'bool', default: true, group: '长期记忆', label: '自动记日志（不判断内容）' },
  },
  setup(bot) {
    const base = () => String(bot.cfg.coreUrl || DEFAULTS.coreUrl).replace(/\/+$/, '')

    // 入站：只记录，不判断；跨 30 分钟由内核结算事件
    bot.on('before-turn', (ev) => {
      if (!bot.cfg.enabled || !bot.cfg.autoLog || !ev?.wake) return
      const person = ev.sender?.id ? 'qq:' + ev.sender.id : ''
      post(base() + '/log', {
        user: String(ev.text || ''),
        reply: '',
        person,
        ts: ev.time ? Number(ev.time) : undefined,
      }).catch((e) => bot.log?.warn?.(`[memory-bridge] 记日志失败：${e?.message || e}`))
    })

    // 工具：名字会被内核加 mcp__qq__ 前缀
    bot.registerTool('memory_recall', {
      description: '翻长期记忆。想提“以前有一次”前先翻；翻不到就说没翻到。',
      parameters: {
        query: { type: 'string', required: true, description: '要找什么' },
        top: { type: 'number', description: '最多几条，默认 5' },
      },
      async execute(args = {}, ctx) {
        const target = args.target || (ctx?.chatKey) || ''
        const person = target.startsWith('private:') ? 'qq:' + target.slice(8) : ''
        try {
          const r = await post(base() + '/recall', {
            q: args.query || '', top: Number(args.top || 5), person, rerank: 'off',
          })
          const d = await r.json()
          const rows = d.results || []
          return {
            ok: true,
            text: rows.length
              ? rows.map((x, i) => `[${i + 1}] ${x.title}\n${String(x.text || '').slice(0, 180)}`).join('\n')
              : '没翻到。',
          }
        } catch (e) {
          return { ok: false, text: `记忆内核连不上（${e?.message || e}），这次不编。` }
        }
      },
    })

    bot.registerTool('memory_context', {
      description: '生成一段可放进上下文的记忆文本（只生成，不自动注入）。',
      parameters: {
        query: { type: 'string', description: '主题（可选）' },
        max_chars: { type: 'number', description: '字符预算，默认 1200' },
      },
      async execute(args = {}) {
        try {
          const r = await post(base() + '/context', {
            q: args.query || '', max_chars: Number(args.max_chars || 1200),
          })
          const d = await r.json()
          return { ok: true, text: d.text || '没有可用的记忆。' }
        } catch (e) {
          return { ok: false, text: `记忆内核连不上（${e?.message || e}）。` }
        }
      },
    })

    bot.registerTool('memory_remember', {
      description: '把值得长期记住的事实写进记忆。纯事实，不写推测。',
      parameters: {
        content: { type: 'string', required: true, description: '要记住的事' },
        title: { type: 'string', description: '短标题（可选）' },
        importance: { type: 'number', description: '0~1（可选）' },
      },
      async execute(args = {}, ctx) {
        const target = args.target || (ctx?.chatKey) || ''
        const person = target.startsWith('private:') ? 'qq:' + target.slice(8) : ''
        try {
          const r = await post(base() + '/remember', {
            title: args.title || '', content: args.content || '', person_id: person,
            importance: args.importance,
          })
          const d = await r.json()
          return { ok: true, text: '已记住。' + (d.id ? ' id=' + d.id : '') }
        } catch (e) {
          return { ok: false, text: `记忆内核连不上（${e?.message || e}）。` }
        }
      },
    })
  },
}
