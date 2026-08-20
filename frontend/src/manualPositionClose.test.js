import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'

const source = readFileSync(new URL('./pages/ActiveTrades.jsx', import.meta.url), 'utf8')

test('active position cards expose a confirmed real close action', () => {
  assert.match(source, /'Close Position'/)
  assert.match(source, /MARKET SELL/)
  assert.match(source, /real Binance Spot Testnet order/)
  assert.match(source, /confirm\(prompt\)/)
})

test('close button is idempotently disabled and canonical state is reconciled', () => {
  assert.match(source, /disabled=\{closing===x\.id\}/)
  assert.match(source, /execution\/positions\/\$\{x\.id\}\/close/)
  assert.match(source, /await api\.post\('\/execution\/reconcile'\)/)
  assert.match(source, /await load\(\)/)
})
