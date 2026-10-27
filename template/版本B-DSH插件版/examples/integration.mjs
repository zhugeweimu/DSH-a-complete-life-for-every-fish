// 集成自检：对运行中的 memory-core 调一次 recall / context
// 用法：node examples/integration.mjs [http://127.0.0.1:8095]
import { apply } from '../lib/index.js'

const base = process.argv[2] || process.env.MEMORY_CORE_URL || 'http://127.0.0.1:8095'
const specs = []
const ctx = { tools: { register: (s) => { specs.push(s); return () => {} } }, on: () => {} }
apply(ctx, { coreUrl: base })

const recall = specs.find((s) => s.name === 'memory_recall')
const context = specs.find((s) => s.name === 'memory_context')

console.log('base =', base)
console.log('tools =', specs.map((s) => s.name).join(', '))
console.log('recall =', JSON.stringify(await recall.execute({ query: '记忆', top: 3 }), null, 1).slice(0, 800))
console.log('context =', JSON.stringify(await context.execute({ query: '记忆', max_chars: 200 }), null, 1).slice(0, 500))
