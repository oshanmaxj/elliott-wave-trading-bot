import test from 'node:test'
import assert from 'node:assert/strict'
import fs from 'node:fs'

const chart = fs.readFileSync(new URL('./components/MarketChart.jsx', import.meta.url), 'utf8')
const api = fs.readFileSync(new URL('./api.js', import.meta.url), 'utf8')

test('analysis and active-position overlays have explicit independent labels', () => {
  assert.match(chart, /ANALYSIS \$\{setup\.direction/)
  assert.match(chart, /ACTIVE \$\{position\.direction/)
  assert.match(chart, /ACTIVE ENTRY/)
  assert.match(chart, /ACTIVE SL/)
})

test('market bundle fetches open position overlays independently by symbol', () => {
  assert.match(api, /execution\/position-overlays/)
  assert.match(api, /activePositions: activePositions\.data/)
})
