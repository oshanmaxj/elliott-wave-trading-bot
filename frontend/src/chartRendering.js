import { toChartCandle } from './marketData.js'

export const validChartPrice = value => Number.isFinite(Number(value)) && Number(value) > 0
export const validChartTime = value => Number.isInteger(value) && value > 0

export const reportRejectedCoordinate = detail => {
  const payload = { component: 'MarketChart', ...detail }
  if (typeof window !== 'undefined') {
    window.dispatchEvent(new CustomEvent('wavescope:chart-coordinate-rejected', { detail: payload }))
    if (import.meta.env?.DEV) window.__WAVESCOPE_CHART_DEBUG__ = [...(window.__WAVESCOPE_CHART_DEBUG__ || []), payload].slice(-500)
  }
  if (import.meta.env?.DEV) console.warn('[WaveScope chart coordinate rejected]', payload)
  return null
}

export const normalizeChartCandles = candles => {
  const valid = candles.map(source => ({ source, chart: toChartCandle(source) })).filter(({ source, chart }) => {
    const pricesValid = [chart.open, chart.high, chart.low, chart.close].every(validChartPrice)
    const envelopeValid = chart.low <= Math.min(chart.open, chart.close) && chart.high >= Math.max(chart.open, chart.close) && chart.low <= chart.high
    if (validChartTime(chart.time) && pricesValid && envelopeValid) return true
    reportRejectedCoordinate({ series_type: 'CandlestickSeries', overlay_type: 'raw_candle', record_id: source.id, timestamp: source.open_time, price: chart, reason: 'invalid_ohlc_coordinate' })
    return false
  }).sort((a, b) => a.chart.time - b.chart.time)
  return [...new Map(valid.map(item => [item.chart.time, item.chart])).values()]
}

export const sanitizeLinePoints = ({ points, overlayType, recordId, candles, requireCandleExtreme = false }) => {
  const candleByTime = new Map(normalizeChartCandles(candles).map(candle => [candle.time, candle]))
  const normalized = points.map(source => ({
    time: typeof (source.timestamp ?? source.time) === 'number'
      ? Math.floor(source.timestamp ?? source.time)
      : Math.floor(new Date(source.timestamp ?? source.time).getTime() / 1000),
    value: Number(source.price ?? source.value), source,
  })).sort((a, b) => a.time - b.time)
  const duplicateTimes = new Set(normalized.filter((point, index, all) =>
    (index > 0 && all[index - 1].time === point.time) || (index + 1 < all.length && all[index + 1].time === point.time)).map(point => point.time))
  return normalized.filter(point => {
    let reason = null
    if (!validChartTime(point.time)) reason = 'invalid_timestamp'
    else if (!validChartPrice(point.value)) reason = 'invalid_price'
    else if (duplicateTimes.has(point.time)) reason = 'duplicate_timestamp'
    else if (requireCandleExtreme) {
      const candle = candleByTime.get(point.time)
      if (!candle) reason = 'timestamp_not_in_candle_series'
      else if (point.value !== candle.high && point.value !== candle.low) reason = 'price_not_canonical_candle_extreme'
    }
    if (!reason) return true
    reportRejectedCoordinate({ series_type: 'LineSeries', overlay_type: overlayType, record_id: recordId, point_id: point.source.id, timestamp: point.source.timestamp ?? point.source.time, price: point.value, reason })
    return false
  }).map(({ time, value }) => ({ time, value }))
}

export const chartRenderPlan = ({ raw, candles, waveCounts = [], activePositions = [], setups = [] }) => {
  const candleData = normalizeChartCandles(candles)
  if (raw) return { candleData, marketDataSeries: 1, overlaySeries: [], priceLines: [] }
  const overlaySeries = waveCounts.map(count => ({ type: 'elliott', id: count.id,
    points: sanitizeLinePoints({ points: count.points || [], overlayType: 'elliott_wave', recordId: count.id, candles, requireCandleExtreme: true })
  })).filter(series => series.points.length >= 2)
  const priceLines = [
    ...setups.flatMap(setup => ['preferred_entry', 'stop_loss', 'take_profit_1', 'take_profit_2', 'take_profit_3'].map(field => ({ overlay_type: 'trade_setup', record_id: setup.id, field, price: setup[field] }))),
    ...activePositions.flatMap(position => ['entry', 'stop_loss', 'take_profit_1', 'take_profit_2', 'take_profit_3'].map(field => ({ overlay_type: 'active_position', record_id: position.position_id, field, price: position[field] }))),
  ].filter(item => validChartPrice(item.price))
  return { candleData, marketDataSeries: 1, overlaySeries, priceLines }
}
