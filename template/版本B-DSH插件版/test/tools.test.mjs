import { test } from 'node:test'
import assert from 'node:assert/strict'
import { apply, name, inject } from '../lib/index.js'

function fakeCtx() {
  const specs = []
  return {
    specs,
    tools: { register: (s) => { specs.push(s); return () => {} } },
    on: () => {},
  }
}

test('插件声明', () => {
  assert.equal(name, 'dsh-memory-core')
  assert.deepEqual(inject, ['tools'])
})

test('注册五个工具', () => {
  const ctx = fakeCtx()
  const app = apply(ctx, { coreUrl: 'http://127.0.0.1:1' })
  const names = ctx.specs.map((s) => s.name)
  assert.deepEqual(names.sort(), [
    'memory_context', 'memory_diary', 'memory_export', 'memory_log', 'memory_recall',
    'memory_remember', 'memory_self', 'memory_sweep',
  ])
  for (const s of ctx.specs) {
    assert.equal(s.parameters.type, 'object')
    assert.equal(typeof s.execute, 'function')
  }
  assert.equal(typeof app.dispose, 'function')
})

test('内核不可达时给出可读失败，不抛', async () => {
  const ctx = fakeCtx()
  apply(ctx, { coreUrl: 'http://127.0.0.1:1' })
  const recall = ctx.specs.find((s) => s.name === 'memory_recall')
  const out = await recall.execute({ query: '带药' })
  assert.equal(out.ok, false)
  assert.match(out.text, /记忆内核连不上/)
})

test('默认参数与 schema 完整', () => {
  const ctx = fakeCtx()
  apply(ctx, {})
  for (const s of ctx.specs) {
    assert.ok(s.description && s.description.length > 5, s.name)
    assert.ok(s.output?.schema?.required?.includes('text'), s.name)
  }
})
