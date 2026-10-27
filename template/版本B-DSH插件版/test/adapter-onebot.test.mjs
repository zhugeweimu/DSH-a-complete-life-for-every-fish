import { test } from 'node:test'
import assert from 'node:assert/strict'
import plugin from '../adapters/dsh-onebot-qq/memory-bridge/index.js'

function fakeBot(cfg = {}) {
  const tools = []
  const hooks = {}
  return {
    cfg: { enabled: true, coreUrl: 'http://127.0.0.1:1', autoLog: true, ...cfg },
    tools,
    hooks,
    log: { info() {}, warn() {}, error() {}, debug() {} },
    on: (stage, fn) => { hooks[stage] = fn; return () => {} },
    registerTool: (name, def) => { tools.push({ name, def }); return () => {} },
    onOutbound: () => () => {},
    onDispose: () => {},
  }
}

test('子插件契约字段', () => {
  assert.equal(plugin.id, 'memory-bridge')
  assert.equal(typeof plugin.setup, 'function')
  assert.ok(plugin.configSchema.coreUrl)
})

test('注册 3 个工具 + before-turn 钩子', () => {
  const bot = fakeBot()
  plugin.setup(bot)
  assert.deepEqual(bot.tools.map((t) => t.name), ['memory_recall', 'memory_context', 'memory_remember'])
  assert.equal(typeof bot.hooks['before-turn'], 'function')
})

test('before-turn 在内核不可达时不抛', () => {
  const bot = fakeBot()
  plugin.setup(bot)
  assert.doesNotThrow(() => bot.hooks['before-turn']({ wake: true, text: '今天好累', sender: { id: '123' } }))
})

test('工具在不可达时返回可读失败', async () => {
  const bot = fakeBot()
  plugin.setup(bot)
  const recall = bot.tools.find((t) => t.name === 'memory_recall')
  const out = await recall.def.execute({ query: '记忆' })
  assert.equal(out.ok, false)
  assert.match(out.text, /连不上/)
})
