import { test } from 'node:test'
import assert from 'node:assert/strict'
import plugin from '../adapters/dsh-aurabot-qq/memory-core/index.mjs'

function fakeApi(settings = {}) {
  const st = { enabled: true, coreUrl: 'http://127.0.0.1:1', autoLog: true, socialReadOnly: true, ...settings }
  const hooks = {}
  const tools = []
  const rules = []
  const log = { info() {}, warn() {}, error() {}, debug() {} }
  const api = {
    id: 'memory-core',
    log,
    settings: () => st,
    saveSettings: () => {},
    on: (ev, fn) => { hooks[ev] = fn; return () => {} },
    defineTool: (spec) => spec,
    tool: (fn) => { tools.push(fn); return () => {} },
    rule: (fn) => { rules.push(fn); return () => {} },
    onDispose: () => {},
  }
  return { api, hooks, tools, rules }
}

test('模块契约字段', () => {
  assert.equal(plugin.id, 'memory-core')
  assert.equal(plugin.api, 1)
  assert.ok(Array.isArray(plugin.settings))
})

test('注册 message 钩子、工具工厂、规则', () => {
  const { api, hooks, tools, rules } = fakeApi()
  plugin.setup(api)
  assert.equal(typeof hooks.message, 'function')
  assert.equal(tools.length, 1)
  assert.equal(rules.length, 1)
  assert.match(rules[0](), /memory_recall/)
})

test('主人私聊给 3 个工具，群聊只给 2 个', () => {
  const { api, tools } = fakeApi()
  plugin.setup(api)
  const full = tools[0]({ level: 'full', chat: { kind: 'private', userId: '1' } })
  const social = tools[0]({ level: 'social', chat: { kind: 'group', groupId: '2' } })
  assert.deepEqual(full.map((t) => t.name), ['memory_recall', 'memory_context', 'memory_remember'])
  assert.deepEqual(social.map((t) => t.name), ['memory_recall', 'memory_context'])
})

test('message 钩子不改接管，且内核不可达不抛', () => {
  const { api, hooks } = fakeApi()
  plugin.setup(api)
  assert.doesNotThrow(() => hooks.message({ text: '今天好累', userId: '123', chatId: 'private:123' }))
  assert.equal(hooks.message({ text: 'x', userId: '1' }), false)
})

test('工具不可达返回可读失败', async () => {
  const { api, tools } = fakeApi()
  plugin.setup(api)
  const full = tools[0]({ level: 'full', chat: { kind: 'private', userId: '1' } })
  const out = await full[0].execute({ query: '记忆' })
  assert.equal(out.ok, false)
  assert.match(out.message, /连不上/)
})
