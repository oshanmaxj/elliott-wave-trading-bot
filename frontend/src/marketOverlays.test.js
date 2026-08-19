import test from 'node:test'
import assert from 'node:assert/strict'
import fs from 'node:fs'

const chart = fs.readFileSync(new URL('./components/MarketChart.jsx', import.meta.url), 'utf8')
const api = fs.readFileSync(new URL('./api.js', import.meta.url), 'utf8')

test('analysis and active-position overlays have explicit independent labels', () => {
  assert.match(chart, /ANALYSIS #\$\{setup\.id\}/)
  assert.match(chart, /ACTIVE \$\{position\.direction/)
  assert.match(chart, /field: 'entry'/)
  assert.match(chart, /field: 'stop_loss'/)
})

test('market bundle fetches open position overlays independently by symbol', () => {
  assert.match(api, /execution\/position-overlays/)
  assert.match(api, /activePositions: uniqueById\(activePositions\.data\)/)
})

test('identical backend overlay ids are deduplicated and raw mode suppresses every overlay family', () => {
  assert.match(api, /uniqueById/)
  assert.ok(chart.indexOf('if (settings.raw)') < chart.indexOf('createSeriesMarkers(candleSeries'))
  assert.ok(chart.indexOf('if (settings.raw)') < chart.indexOf('const addPriceLine'))
})
