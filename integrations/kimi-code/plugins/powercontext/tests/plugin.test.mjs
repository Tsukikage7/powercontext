/*
 * Copyright (c) 2026 OceanBase.
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 * http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 */

import assert from 'node:assert/strict'
import { spawn } from 'node:child_process'
import { cp, mkdtemp, readFile, rm, writeFile } from 'node:fs/promises'
import http from 'node:http'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import test from 'node:test'
import { fileURLToPath } from 'node:url'

const plugin = fileURLToPath(new URL('..', import.meta.url))
const prompt = 'The deployment color is teal-731.'
async function fixture(t, overrides = {}, settings = {}, listenHost = '127.0.0.1') {
  const root = await mkdtemp(join(tmpdir(), 'pc-kimi-'))
  const installed = join(root, 'plugin')
  await cp(plugin, installed, { recursive: true, filter: path => !path.includes('tests') })
  const captures = []
  const requests = []
  const server = http.createServer(async (req, res) => {
    let raw = ''
    for await (const chunk of req) raw += chunk
    const body = raw ? JSON.parse(raw) : undefined
    requests.push({ path: req.url, body, authorization: req.headers.authorization })
    if (overrides[req.url]) return overrides[req.url](req, res, body)
    const answer = req.url === '/v1/scope-bindings/resolve' ? { scope_id: 'scope-test' } :
      req.url === '/v1/context/prepare' ? { schema: 'powercontext.prepared-context.v1', status: 'ready',
        content: 'Historical context: use uv.', content_bytes: Buffer.byteLength('Historical context: use uv.') } :
      req.url === '/health/ready' ? { status: 'ready' } : undefined
    if (answer) { res.writeHead(200); res.end(JSON.stringify(answer)); return }
    if (req.url === '/v1/sources/content') {
      captures.push(body)
      res.writeHead(202)
      res.end(JSON.stringify({ status: 'accepted', source: { name: 'content', source_id: body.source_id }, position: captures.length }))
      return
    }
    res.writeHead(404); res.end('{}')
  })
  await new Promise(resolve => server.listen(0, listenHost, resolve))
  await writeFile(join(installed, 'powercontext.json'), JSON.stringify({
    server_url: 'http://' + listenHost + ':' + server.address().port, scope_id: 'scope-test', capture_prompts: true, use_token: false, ...settings,
  }))
  t.after(async () => { server.closeAllConnections(); await new Promise(resolve => server.close(resolve)); await rm(root, { recursive: true, force: true }) })
  async function invoke(input = { hook_event_name: 'UserPromptSubmit', session_id: 'session-test', cwd: '/workspace', prompt },
    environment = {}) {
    const env = { ...process.env, ...environment }
    if (!Object.hasOwn(environment, 'POWERCONTEXT_KIMI_TOKEN')) delete env.POWERCONTEXT_KIMI_TOKEN
    const child = spawn(process.execPath, [join(installed, 'hooks/user_prompt_submit.mjs')], { cwd: installed,
      env, windowsHide: true, stdio: ['pipe', 'pipe', 'pipe'] })
    let stdout = '', stderr = ''
    child.stdout.on('data', chunk => stdout += chunk)
    child.stderr.on('data', chunk => stderr += chunk)
    child.stdin.end(JSON.stringify(input))
    const code = await new Promise(resolve => child.on('exit', resolve))
    assert.equal(code, 0, stderr)
    return { output: JSON.parse(stdout), stderr }
  }
  return { captures, requests, invoke, installed }
}

test('context and capture use the explicit Scope and shared runtime token', async t => {
  const f = await fixture(t, {}, { use_token: true })
  const { output, stderr } = await f.invoke(undefined, { POWERCONTEXT_KIMI_TOKEN: 'synthetic-token' })
  assert.match(output.message, /scope-test/)
  assert.match(output.message, /Historical context: use uv/)
  assert.equal(stderr, '')
  assert.equal(f.captures[0].content, prompt)
  assert.equal(f.captures[0].scope_id, 'scope-test')
  assert.equal(f.captures[0].metadata.session_id, 'session-test')
  assert.ok(f.requests.every(r => r.authorization === 'Bearer synthetic-token'))
  assert.deepEqual(f.requests[0].body, { explicit_scope_id: 'scope-test', binding_keys: [] })
  await f.invoke(undefined, { POWERCONTEXT_KIMI_TOKEN: 'synthetic-token' })
  assert.notEqual(f.captures[0].source_id, f.captures[1].source_id)
})

test('disabled capture still supplies context', async t => {
  const f = await fixture(t, {}, { capture_prompts: false })
  assert.match((await f.invoke()).output.message, /Historical context/)
  assert.deepEqual(f.captures, [])
})

test('sensitive text and the configured token are never captured', async t => {
  const f = await fixture(t, {}, { use_token: true })
  await f.invoke({ hook_event_name: 'UserPromptSubmit', prompt: 'api_key=private-value' }, { POWERCONTEXT_KIMI_TOKEN: 'synthetic-token' })
  await f.invoke({ hook_event_name: 'UserPromptSubmit', prompt: 'my opaque synthetic-token value' },
    { POWERCONTEXT_KIMI_TOKEN: 'synthetic-token' })
  assert.deepEqual(f.captures, [])
})

test('multimodal input captures text only and pure media is ignored', async t => {
  const f = await fixture(t)
  await f.invoke({ hook_event_name: 'UserPromptSubmit', prompt: [{ type: 'image', data: 'private-image' }, { type: 'text', text: prompt }] })
  assert.equal(f.captures[0].content, prompt)
  assert.deepEqual((await f.invoke({ hook_event_name: 'UserPromptSubmit', prompt: [{ type: 'image', data: 'private-image' }] })).output, {})
  assert.equal(f.captures.length, 1)
})

test('scope rejection never redirects writes and reports no secrets', async t => {
  const f = await fixture(t, { '/v1/scope-bindings/resolve': (_req, res) => { res.writeHead(401); res.end('private response') } })
  const { output, stderr } = await f.invoke()
  assert.deepEqual(output, {})
  assert.deepEqual(f.captures, [])
  assert.match(stderr, /authentication_failed/)
  assert.doesNotMatch(stderr, /private response|teal-731|127\.0\.0\.1/)
})

test('invalid prepared context is withheld while capture remains usable', async t => {
  const f = await fixture(t, { '/v1/context/prepare': (_req, res) => {
    res.writeHead(200); res.end(JSON.stringify({ schema: 'powercontext.prepared-context.v1', status: 'ready',
      content: 'bad history', content_bytes: 1 }))
  } })
  const { output, stderr } = await f.invoke()
  assert.doesNotMatch(output.message, /bad history/)
  assert.match(stderr, /invalid_response/)
  assert.equal(f.captures.length, 1)
})

test('an accepted write with a lost reply stays unknown without automatic retry', async t => {
  const writes = []
  const f = await fixture(t, { '/v1/sources/content': (req, _res, body) => { writes.push(body); req.socket.destroy() } })
  const { output, stderr } = await f.invoke()
  assert.match(output.message, /Historical context/)
  assert.equal(writes.length, 1)
  assert.equal(JSON.parse(stderr.trim()).result, 'unknown')
})

test('timeouts fail open within the automatic budget', async t => {
  const f = await fixture(t, { '/v1/scope-bindings/resolve': () => {} })
  const started = Date.now()
  const { output, stderr } = await f.invoke()
  assert.deepEqual(output, {})
  assert.match(stderr, /server_unavailable/)
  assert.ok(Date.now() - started < 5000)
})

test('loopback aliases support automatic context and Source capture', async t => {
  const f = await fixture(t, {}, {}, '127.0.0.2')
  const { output, stderr } = await f.invoke()
  assert.match(output.message, /Historical context: use uv/)
  assert.equal(stderr, '')
  assert.equal(f.captures[0].content, prompt)
  assert.equal(f.captures[0].scope_id, 'scope-test')
})

test('non-loopback plaintext settings are rejected before sending requests', async t => {
  const f = await fixture(t, {}, { server_url: 'http://127.0.0.1.example.test:8000' })
  const { output, stderr } = await f.invoke()
  assert.deepEqual(output, {})
  assert.match(stderr, /invalid_settings/)
  assert.deepEqual(f.requests, [])
})
