import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'

const source = readFileSync(new URL('./pages/ExecutionPages.jsx', import.meta.url), 'utf8')

test('pending approval cards expose approve reject and details actions', () => {
  assert.match(source, /'Approve'/)
  assert.match(source, /'Reject'/)
  assert.match(source, />View Details</)
  assert.match(source, /take_profit_1/)
  assert.match(source, /risk_reward_1/)
})

test('approval actions reuse existing csrf-protected execution endpoints', () => {
  assert.match(source, /execution\/setups\/\$\{id\}\/\$\{action\}/)
  assert.match(source, /disabled=\{busy\?\.id===x\.id\}/)
  assert.match(source, /await refresh\(\)/)
})

test('approve and reject show a per-row busy label and a distinct success/failure color', () => {
  assert.match(source, /busy\?\.id===x\.id&&busy\.action==='approve'\?'Approving…'/)
  assert.match(source, /busy\?\.id===x\.id&&busy\.action==='reject'\?'Rejecting…'/)
  assert.match(source, /result\.ok\?'notice good':'alert'/)
})

test('automatic mode never labels eligible setups as awaiting approval', () => {
  assert.match(source, /Automatic Testnet Execution/)
  assert.match(source, /They are not awaiting approval/)
  assert.match(source, /manual=\{s\.data\.manual_approval_required\}/)
})
