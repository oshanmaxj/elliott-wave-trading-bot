import test from 'node:test'
import assert from 'node:assert/strict'
import fs from 'node:fs'
import { chartRenderPlan, deriveHeikinAshiCandles, normalizeChartCandles, sanitizeLinePoints } from './chartRendering.js'

const candles = [
  { id: 1, open_time: '2026-08-18T00:00:00Z', open: '100', high: '110', low: '90', close: '105' },
  { id: 2, open_time: '2026-08-18T00:01:00Z', open: '105', high: '115', low: '95', close: '110' },
]
const wave = { id: 7, points: [
  { id: 70, timestamp: candles[0].open_time, price: '90' },
  { id: 71, timestamp: candles[1].open_time, price: '115' },
] }

test('raw mode plans exactly one candlestick series and zero overlays', () => {
  const plan = chartRenderPlan({ raw: true, candles, waveCounts: [wave],
    activePositions: [{ position_id: 9, entry: 100 }], setups: [{ id: 8, stop_loss: 90 }] })
  assert.equal(plan.marketDataSeries, 1)
  assert.deepEqual(plan.overlaySeries, [])
  assert.deepEqual(plan.priceLines, [])
})

test('normal mode retains valid Elliott and active-position overlays', () => {
  const plan = chartRenderPlan({ raw: false, candles, waveCounts: [wave],
    activePositions: [{ position_id: 9, entry: 100, stop_loss: 90 }] })
  assert.equal(plan.overlaySeries.length, 1)
  assert.equal(plan.priceLines.filter(line => line.overlay_type === 'active_position').length, 2)
})

test('switching normal to raw removes the previously planned overlays', () => {
  const normal = chartRenderPlan({ raw: false, candles, waveCounts: [wave] })
  const raw = chartRenderPlan({ raw: true, candles, waveCounts: [wave] })
  assert.equal(normal.overlaySeries.length, 1)
  assert.equal(raw.overlaySeries.length, 0)
})

test('switching symbol or timeframe produces only coordinates from the new candle set', () => {
  const eth = candles.map((row, index) => ({ ...row, id: index + 20,
    open_time: `2026-08-19T00:0${index}:00Z`, low: '190', high: '210', open: '200', close: '205' }))
  const plan = chartRenderPlan({ raw: true, candles: eth })
  assert.deepEqual(plan.candleData.map(row => row.time), [1787097600, 1787097660])
  assert.equal(plan.candleData.some(row => row.time === 1787011200), false)
})

test('null undefined zero and duplicate-time Elliott coordinates cannot form lines', () => {
  const points = sanitizeLinePoints({ candles, overlayType: 'elliott_wave', recordId: 7,
    requireCandleExtreme: true, points: [
      { id: 1, timestamp: candles[0].open_time, price: 0 },
      { id: 2, timestamp: candles[1].open_time, price: null },
      { id: 3, timestamp: candles[1].open_time, price: 115 },
      { id: 4, timestamp: candles[1].open_time, price: 95 },
      { id: 5, timestamp: undefined, price: 100 },
    ] })
  assert.deepEqual(points, [])
})

test('overlay planning never mutates candle OHLC values', () => {
  const before = structuredClone(candles)
  chartRenderPlan({ raw: false, candles, waveCounts: [wave] })
  normalizeChartCandles(candles)
  assert.deepEqual(candles, before)
})

test('Heikin Ashi formula and deterministic chronological initialization', () => {
  const real = [
    { id: 1, open_time: '2026-08-18T00:00:00Z', open: '10', high: '14', low: '8', close: '12' },
    { id: 2, open_time: '2026-08-18T00:01:00Z', open: '12', high: '15', low: '10', close: '14' },
  ]
  const ha = deriveHeikinAshiCandles(real)
  assert.equal(ha[0].open, 11); assert.equal(ha[0].close, 11); assert.equal(ha[0].high, 14); assert.equal(ha[0].low, 8)
  assert.equal(ha[1].open, 11); assert.equal(ha[1].close, 12.75); assert.equal(ha[1].high, 15); assert.equal(ha[1].low, 10)
})

test('Heikin Ashi candles never mutate the source real OHLC values', () => {
  const real = [{ id: 1, open_time: '2026-08-18T00:00:00Z', open: '10', high: '14', low: '8', close: '12' }]
  const before = structuredClone(real)
  deriveHeikinAshiCandles(real)
  assert.deepEqual(real, before)
})

test('MarketChart raw boundary precedes every overlay-construction API', () => {
  const source = fs.readFileSync(new URL('./components/MarketChart.jsx', import.meta.url), 'utf8')
  const boundary = source.indexOf('if (settings.raw)')
  assert.ok(boundary > source.indexOf('chart.addSeries(CandlestickSeries'))
  assert.ok(boundary < source.indexOf('createSeriesMarkers(candleSeries'))
  assert.ok(boundary < source.indexOf('const addPriceLine'))
  assert.match(source, /container\.replaceChildren\(\)/)
  assert.match(source, /\[chartKey, candles/)
})
