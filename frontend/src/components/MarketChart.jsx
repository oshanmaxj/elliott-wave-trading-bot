import { useEffect, useRef } from 'react'
import { CandlestickSeries, ColorType, LineSeries, createChart, createSeriesMarkers } from 'lightweight-charts'

const seconds = value => Math.floor(new Date(value).getTime() / 1000)

export default function MarketChart({ candles, swings, structure, fvg, indicators }) {
  const host = useRef(null)
  useEffect(() => {
    if (!host.current || !candles.length) return
    const chart = createChart(host.current, {
      autoSize: true, layout: { background: { type: ColorType.Solid, color: '#0c121c' }, textColor: '#8c9aab', fontFamily: 'Inter, sans-serif' },
      grid: { vertLines: { color: '#18212e' }, horzLines: { color: '#18212e' } },
      rightPriceScale: { borderColor: '#253144' }, timeScale: { borderColor: '#253144', timeVisible: true },
      crosshair: { vertLine: { color: '#4c6381' }, horzLine: { color: '#4c6381' } },
    })
    const candleSeries = chart.addSeries(CandlestickSeries, { upColor: '#20c997', downColor: '#ef5b5b', wickUpColor: '#20c997', wickDownColor: '#ef5b5b', borderVisible: false })
    const candleById = Object.fromEntries(candles.map(c => [c.id, c]))
    candleSeries.setData(candles.map(c => ({ time: seconds(c.open_time), open: +c.open, high: +c.high, low: +c.low, close: +c.close })))
    const colors = { ema20: '#ffcc66', ema50: '#a78bfa', ema200: '#4ea8de' }
    Object.entries(colors).forEach(([key, color]) => {
      const values = candles.map(c => c.indicators?.[key]).filter(Boolean)
      if (values.length) { const series = chart.addSeries(LineSeries, { color, lineWidth: 1, priceLineVisible: false, lastValueVisible: false }); series.setData(values) }
    })
    const markers = [
      ...swings.map(s => { const c = candleById[s.candle_id]; return c && { time: seconds(c.open_time), position: s.swing_type === 'high' ? 'aboveBar' : 'belowBar', color: s.swing_type === 'high' ? '#ffcc66' : '#57d7ff', shape: s.swing_type === 'high' ? 'arrowDown' : 'arrowUp', text: s.swing_type === 'high' ? 'SH' : 'SL' } }),
      ...structure.map(e => { const c = candleById[e.confirmation_candle_id]; return c && { time: seconds(c.open_time), position: e.direction === 'bullish' ? 'belowBar' : 'aboveBar', color: e.event_type === 'CHoCH' ? '#f59eeb' : '#a3e635', shape: 'circle', text: e.event_type } }),
    ].filter(Boolean).sort((a,b) => a.time - b.time)
    createSeriesMarkers(candleSeries, markers)
    fvg.filter(z => ['active','partially_mitigated'].includes(z.status)).slice(-8).forEach(z => {
      candleSeries.createPriceLine({ price: +z.upper_price, color: z.direction === 'bullish' ? '#20c99788' : '#ef5b5b88', lineWidth: 1, lineStyle: 2, axisLabelVisible: false, title: `${z.direction === 'bullish' ? 'B' : 'S'} FVG` })
      candleSeries.createPriceLine({ price: +z.lower_price, color: z.direction === 'bullish' ? '#20c99788' : '#ef5b5b88', lineWidth: 1, lineStyle: 2, axisLabelVisible: false })
    })
    chart.timeScale().fitContent()
    return () => chart.remove()
  }, [candles, swings, structure, fvg, indicators])
  return <div className="chart" ref={host} />
}

