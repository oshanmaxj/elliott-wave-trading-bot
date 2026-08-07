import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

import { TIMEFRAMES } from './constants.js'

test('lower timeframe selectors share the complete central list', () => {
  assert.deepEqual([...TIMEFRAMES], ['1m', '5m', '15m', '1h', '4h'])
  for (const page of [
    'MarketAnalysis.jsx',
    'DataPages.jsx',
    'ElliottWave.jsx',
    'OpportunityPages.jsx',
    'ExecutionPages.jsx',
  ]) {
    const source = readFileSync(new URL(`./pages/${page}`, import.meta.url), 'utf8')
    assert.match(source, /TIMEFRAMES/)
  }
  const strategies = readFileSync(
    new URL('./pages/ExecutionPages.jsx', import.meta.url),
    'utf8',
  )
  assert.match(strategies, /Trading Timeframes/)
  assert.match(strategies, /enabled_timeframes_json/)
})
