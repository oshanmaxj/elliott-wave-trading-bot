import { useEffect, useRef } from 'react'
import { CandlestickSeries, ColorType, LineSeries, createChart, createSeriesMarkers } from 'lightweight-charts'

const seconds = value => Math.floor(new Date(value).getTime() / 1000)

export default function MarketChart({ candles, swings, structure, fvg, liquidity, orderBlocks, premiumDiscount, indicators, settings }) {
  const host = useRef(null)
  useEffect(() => {
    if (!host.current || !candles.length) return
    const container = host.current
    const chart = createChart(container, {
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
    const markerApi = createSeriesMarkers(candleSeries, [])
    const renderMarkers = showInternal => {
      const major = swings.filter(s => +s.strength >= .5)
      const visibleSwings = [...(settings.majorSwings ? major : []), ...(showInternal ? swings.filter(s => +s.strength < .5) : [])]
      const raw = [
        ...visibleSwings.map(s => { const c = candleById[s.candle_id]; return c && { time: seconds(c.open_time), position: s.swing_type === 'high' ? 'aboveBar' : 'belowBar', color: s.swing_type === 'high' ? '#ffcc66' : '#57d7ff', shape: s.swing_type === 'high' ? 'arrowDown' : 'arrowUp', text: +s.strength >= .5 ? (s.swing_type === 'high' ? 'SH' : 'SL') : (s.swing_type === 'high' ? 'iH' : 'iL') } }),
        ...structure.filter(e => (e.event_type === 'BOS' && settings.bos) || (e.event_type === 'CHoCH' && settings.choch)).map(e => { const c = candleById[e.confirmation_candle_id]; return c && { time: seconds(c.open_time), position: e.direction === 'bullish' ? 'belowBar' : 'aboveBar', color: e.event_type === 'CHoCH' ? '#f59eeb' : '#a3e635', shape: 'circle', text: e.event_type } }),
      ].filter(Boolean)
      const step = candles.length > 1 ? seconds(candles[1].open_time) - seconds(candles[0].open_time) : 0
      const clustered = raw.sort((a,b) => a.time - b.time).reduce((groups, marker) => { const nearby = [...groups].reverse().find(item => item.position === marker.position && marker.time - item.time <= step); if (nearby) nearby.text += ` · ${marker.text}`; else groups.push({...marker}); return groups }, [])
      markerApi.setMarkers(clustered)
    }
    const bands = []
    const addBand = (top, bottom, className, label) => {
      const element = document.createElement('div')
      element.className = `price-band ${className}`
      element.textContent = label
      container.appendChild(element)
      bands.push({ element, top: +top, bottom: +bottom })
    }
    const updateBands = () => bands.forEach(band => {
      const top = candleSeries.priceToCoordinate(band.top), bottom = candleSeries.priceToCoordinate(band.bottom)
      if (top == null || bottom == null) { band.element.style.display = 'none'; return }
      band.element.style.display = 'block'; band.element.style.top = `${Math.min(top,bottom)}px`; band.element.style.height = `${Math.abs(bottom-top)}px`
    })
    renderMarkers(false)
    chart.timeScale().subscribeVisibleLogicalRangeChange(range => { renderMarkers(Boolean(settings.internalSwings && range && range.to - range.from <= 80)); updateBands() })
    settings.fvg && fvg.filter(z => ['active','partially_mitigated'].includes(z.status)).slice(-8).forEach(z => {
      candleSeries.createPriceLine({ price: +z.upper_price, color: z.direction === 'bullish' ? '#20c99788' : '#ef5b5b88', lineWidth: 1, lineStyle: 2, axisLabelVisible: false, title: `${z.direction === 'bullish' ? 'B' : 'S'} FVG` })
      candleSeries.createPriceLine({ price: +z.lower_price, color: z.direction === 'bullish' ? '#20c99788' : '#ef5b5b88', lineWidth: 1, lineStyle: 2, axisLabelVisible: false })
    })
    settings.liquidity && liquidity.filter(pool => !pool.swept_at).slice(-8).forEach(pool => candleSeries.createPriceLine({ price: +pool.price, color: '#fbbf2488', lineWidth: 1, lineStyle: 2, axisLabelVisible: true, title: pool.type }))
    settings.orderBlocks && orderBlocks.filter(block => ['active','partially_mitigated'].includes(block.status)).slice(-5).forEach(block => {
      addBand(block.top_price, block.bottom_price, block.direction === 'bullish' ? 'ob-bullish' : 'ob-bearish', `${block.direction === 'bullish' ? 'Bull' : 'Bear'} OB`)
      candleSeries.createPriceLine({ price: +block.top_price, color: block.direction === 'bullish' ? '#20c99788' : '#ef5b5b88', lineWidth: 1, lineStyle: 1, axisLabelVisible: false, title: `${block.direction === 'bullish' ? 'Bull' : 'Bear'} OB` })
      candleSeries.createPriceLine({ price: +block.bottom_price, color: block.direction === 'bullish' ? '#20c99788' : '#ef5b5b88', lineWidth: 1, lineStyle: 1, axisLabelVisible: false })
    })
    if (premiumDiscount) {
      addBand(premiumDiscount.premium.top, premiumDiscount.premium.bottom, 'premium', 'PREMIUM')
      addBand(premiumDiscount.discount.top, premiumDiscount.discount.bottom, 'discount', 'DISCOUNT')
      candleSeries.createPriceLine({ price: +premiumDiscount.equilibrium, color: '#94a3b8aa', lineWidth: 1, lineStyle: 2, axisLabelVisible: true, title: 'EQ 50%' })
    }
    chart.timeScale().fitContent()
    requestAnimationFrame(updateBands)
    return () => { bands.forEach(band => band.element.remove()); chart.remove() }
  }, [candles, swings, structure, fvg, liquidity, orderBlocks, premiumDiscount, indicators, settings])
  return <div className="chart" ref={host} />
}
