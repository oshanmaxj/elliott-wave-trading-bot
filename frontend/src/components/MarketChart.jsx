import { useEffect, useRef } from 'react'
import { CandlestickSeries, ColorType, LineSeries, createChart, createSeriesMarkers } from 'lightweight-charts'
import { normalizeChartCandles, reportRejectedCoordinate, sanitizeLinePoints, validChartPrice } from '../chartRendering'

const seconds = value => Math.floor(new Date(value).getTime() / 1000)

export default function MarketChart({ chartKey, candles, swings, structure, fvg, liquidity, orderBlocks, premiumDiscount, sweeps, setups, activePositions = [], waveCounts, settings, onLoadOlder }) {
  const host = useRef(null)
  const loadingOlder = useRef(false)

  useEffect(() => {
    if (!host.current || !candles.length) return
    const container = host.current
    // Remove any orphaned canvas/HTML primitives left by an interrupted React render.
    container.replaceChildren()
    const chart = createChart(container, {
      autoSize: true,
      layout: { background: { type: ColorType.Solid, color: '#0c121c' }, textColor: '#8c9aab', fontFamily: 'Inter, sans-serif' },
      grid: { vertLines: { color: '#18212e' }, horzLines: { color: '#18212e' } },
      rightPriceScale: { borderColor: '#253144' },
      timeScale: { borderColor: '#253144', timeVisible: true },
      crosshair: { vertLine: { color: '#4c6381' }, horzLine: { color: '#4c6381' } },
    })
    const candleSeries = chart.addSeries(CandlestickSeries, { upColor: '#20c997', downColor: '#ef5b5b', wickUpColor: '#20c997', wickDownColor: '#ef5b5b', borderVisible: false })
    candleSeries.setData(normalizeChartCandles(candles))

    const cleanup = bands => {
      bands.forEach(band => band.element.remove())
      chart.remove()
      container.replaceChildren()
    }

    // RAW ISOLATION BOUNDARY: no marker API, line series, price line, band,
    // primitive, or analytical callback is constructed beyond this point.
    if (settings.raw) {
      chart.timeScale().fitContent()
      return () => cleanup([])
    }

    const candleById = Object.fromEntries(candles.map(candle => [candle.id, candle]))
    const bands = []
    const addPriceLine = (price, options, metadata) => {
      if (!validChartPrice(price)) {
        reportRejectedCoordinate({ series_type: 'PriceLine', ...metadata, price, reason: 'invalid_price_coordinate' })
        return null
      }
      return candleSeries.createPriceLine({ price: Number(price), ...options })
    }
    const addBand = (top, bottom, className, label, metadata) => {
      if (!validChartPrice(top) || !validChartPrice(bottom)) {
        reportRejectedCoordinate({ series_type: 'HtmlBand', ...metadata, price: { top, bottom }, reason: 'invalid_band_coordinate' })
        return
      }
      const element = document.createElement('div')
      element.className = `price-band ${className}`
      element.textContent = label
      container.appendChild(element)
      bands.push({ element, top: Number(top), bottom: Number(bottom) })
    }
    const updateBands = () => bands.forEach(band => {
      const top = candleSeries.priceToCoordinate(band.top)
      const bottom = candleSeries.priceToCoordinate(band.bottom)
      if (top == null || bottom == null) { band.element.style.display = 'none'; return }
      band.element.style.display = 'block'
      band.element.style.top = `${Math.min(top, bottom)}px`
      band.element.style.height = `${Math.abs(bottom - top)}px`
    })

    const colors = { ema20: '#ffcc66', ema50: '#a78bfa', ema200: '#4ea8de' }
    Object.entries(colors).forEach(([key, color]) => {
      const points = sanitizeLinePoints({ points: candles.map(c => c.indicators?.[key]).filter(Boolean), overlayType: key, recordId: chartKey, candles })
      if (points.length >= 2) {
        const series = chart.addSeries(LineSeries, { color, lineWidth: 1, priceLineVisible: false, lastValueVisible: false })
        series.setData(points)
      }
    })

    const markerApi = createSeriesMarkers(candleSeries, [])
    const renderMarkers = showInternal => {
      const major = swings.filter(s => Number(s.strength) >= 0.5)
      const visibleSwings = [...(settings.majorSwings ? major : []), ...(showInternal ? swings.filter(s => Number(s.strength) < 0.5) : [])]
      const markers = [
        ...visibleSwings.map(s => { const c = candleById[s.candle_id]; return c && { time: seconds(c.open_time), position: s.swing_type === 'high' ? 'aboveBar' : 'belowBar', color: s.swing_type === 'high' ? '#ffcc66' : '#57d7ff', shape: s.swing_type === 'high' ? 'arrowDown' : 'arrowUp', text: Number(s.strength) >= 0.5 ? (s.swing_type === 'high' ? 'SH' : 'SL') : (s.swing_type === 'high' ? 'iH' : 'iL') } }),
        ...structure.filter(e => (e.event_type === 'BOS' && settings.bos) || (e.event_type === 'CHoCH' && settings.choch)).map(e => { const c = candleById[e.confirmation_candle_id]; return c && { time: seconds(c.open_time), position: e.direction === 'bullish' ? 'belowBar' : 'aboveBar', color: e.event_type === 'CHoCH' ? '#f59eeb' : '#a3e635', shape: 'circle', text: e.event_type } }),
        ...sweeps.filter(s => settings.sweeps && s.status === 'confirmed').flatMap(s => { const swept = candleById[s.sweep_candle_id]; const confirmed = candleById[s.confirmation_candle_id]; return [swept && { time: seconds(swept.open_time), position: s.direction === 'bullish' ? 'belowBar' : 'aboveBar', color: '#fbbf24', shape: s.direction === 'bullish' ? 'arrowUp' : 'arrowDown', text: s.direction === 'bullish' ? 'SSL SWEEP' : 'BSL SWEEP' }, confirmed && confirmed.id !== swept?.id && { time: seconds(confirmed.open_time), position: s.direction === 'bullish' ? 'belowBar' : 'aboveBar', color: '#22d3ee', shape: 'circle', text: 'RECLAIM' }] }),
        ...setups.filter(s => settings.setups && ['ready', 'triggered'].includes(s.status)).map(setup => { const event = structure.find(item => item.id === setup.structure_event_id); const c = event && candleById[event.confirmation_candle_id]; return c && { time: seconds(c.open_time), position: setup.direction === 'bullish' ? 'belowBar' : 'aboveBar', color: '#c084fc', shape: 'square', text: `ANALYSIS ${setup.direction === 'bullish' ? 'LONG' : 'SHORT'} #${setup.id}` } }),
      ].filter(marker => marker && Number.isInteger(marker.time) && marker.time > 0).sort((a, b) => a.time - b.time)
      markerApi.setMarkers(markers)
    }
    renderMarkers(false)

    const rangeHandler = range => {
      renderMarkers(Boolean(settings.internalSwings && range && range.to - range.from <= 80))
      updateBands()
      if (range && range.from < 20 && onLoadOlder && !loadingOlder.current) {
        loadingOlder.current = true
        Promise.resolve(onLoadOlder()).finally(() => { loadingOlder.current = false })
      }
    }
    chart.timeScale().subscribeVisibleLogicalRangeChange(rangeHandler)

    if (settings.fvg) fvg.filter(z => ['active', 'partially_mitigated'].includes(z.status)).slice(-8).forEach(z => {
      addPriceLine(z.upper_price, { color: z.direction === 'bullish' ? '#20c99788' : '#ef5b5b88', lineWidth: 1, lineStyle: 2, axisLabelVisible: false, title: `${z.direction === 'bullish' ? 'B' : 'S'} FVG` }, { overlay_type: 'fvg', record_id: z.id, timestamp: z.detected_at })
      addPriceLine(z.lower_price, { color: z.direction === 'bullish' ? '#20c99788' : '#ef5b5b88', lineWidth: 1, lineStyle: 2, axisLabelVisible: false }, { overlay_type: 'fvg', record_id: z.id, timestamp: z.detected_at })
    })
    if (settings.liquidity) liquidity.filter(pool => !pool.swept_at).slice(-8).forEach(pool => addPriceLine(pool.price, { color: '#fbbf2488', lineWidth: 1, lineStyle: 2, axisLabelVisible: true, title: pool.type }, { overlay_type: 'liquidity_pool', record_id: pool.id, timestamp: pool.detected_at }))
    if (settings.orderBlocks) orderBlocks.filter(block => ['active', 'partially_mitigated'].includes(block.status)).slice(-5).forEach(block => {
      const meta = { overlay_type: 'order_block', record_id: block.id, timestamp: block.detected_at }
      addBand(block.top_price, block.bottom_price, block.direction === 'bullish' ? 'ob-bullish' : 'ob-bearish', `${block.direction === 'bullish' ? 'Bull' : 'Bear'} OB`, meta)
      addPriceLine(block.top_price, { color: block.direction === 'bullish' ? '#20c99788' : '#ef5b5b88', lineWidth: 1, lineStyle: 1, axisLabelVisible: false, title: 'OB' }, meta)
      addPriceLine(block.bottom_price, { color: block.direction === 'bullish' ? '#20c99788' : '#ef5b5b88', lineWidth: 1, lineStyle: 1, axisLabelVisible: false }, meta)
    })
    if (premiumDiscount) {
      addBand(premiumDiscount.premium.top, premiumDiscount.premium.bottom, 'premium', 'PREMIUM', { overlay_type: 'premium_discount' })
      addBand(premiumDiscount.discount.top, premiumDiscount.discount.bottom, 'discount', 'DISCOUNT', { overlay_type: 'premium_discount' })
      addPriceLine(premiumDiscount.equilibrium, { color: '#94a3b8aa', lineWidth: 1, lineStyle: 2, axisLabelVisible: true, title: 'EQ 50%' }, { overlay_type: 'premium_discount' })
    }

    waveCounts.filter(count => (count.status === 'primary' && settings.primaryWave) || (count.status === 'alternate' && settings.alternateWaves)).slice(0, 3).forEach(count => {
      const points = sanitizeLinePoints({ points: count.points || [], overlayType: 'elliott_wave', recordId: count.id, candles, requireCandleExtreme: true })
      if (points.length < 2) return
      const primary = count.status === 'primary'; const color = primary ? '#f8fafc' : '#64748b'
      const series = chart.addSeries(LineSeries, { color, lineWidth: primary ? 2 : 1, lineStyle: primary ? 0 : 2, priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false })
      series.setData(points)
      const labels = new Map((count.points || []).map(point => [seconds(point.timestamp), point.wave_label]))
      createSeriesMarkers(series, points.map(point => ({ time: point.time, position: 'inBar', color, shape: 'circle', text: labels.get(point.time) || '' })))
      if (primary && settings.fibonacci) addPriceLine(count.invalidation_price, { color: '#ef4444', lineWidth: 1, lineStyle: 2, axisLabelVisible: true, title: 'WAVE INVALIDATION' }, { overlay_type: 'elliott_invalidation', record_id: count.id })
      if (primary && settings.waveTargets) addBand(count.projected_target_max, count.projected_target_min, 'wave-target', `WAVE ${count.metadata_json?.current_wave || ''} TARGET`, { overlay_type: 'elliott_target', record_id: count.id })
    })

    setups.filter(setup => settings.setups && ['ready', 'triggered'].includes(setup.status)).forEach(setup => {
      const prefix = `ANALYSIS #${setup.id}`; const meta = { overlay_type: 'trade_setup', record_id: setup.id, timestamp: setup.detected_at }
      if (settings.entryZones) addBand(setup.entry_max, setup.entry_min, 'setup-entry', `${prefix} ENTRY`, meta)
      if (settings.entryZones) addPriceLine(setup.preferred_entry, { color: '#c084fc', lineWidth: 2, lineStyle: 0, axisLabelVisible: true, title: `${prefix} ENTRY` }, { ...meta, field: 'preferred_entry' })
      if (settings.entryZones) addPriceLine(setup.stop_loss, { color: '#ef708f', lineWidth: 2, lineStyle: 0, axisLabelVisible: true, title: `${prefix} SL` }, { ...meta, field: 'stop_loss' })
      if (settings.targets) [setup.take_profit_1, setup.take_profit_2, setup.take_profit_3].forEach((target, index) => addPriceLine(target, { color: '#86efac', lineWidth: 1, lineStyle: 2, axisLabelVisible: true, title: `${prefix} TP${index + 1}` }, { ...meta, field: `take_profit_${index + 1}` }))
    })
    activePositions.forEach(position => {
      const prefix = `ACTIVE ${position.direction === 'bullish' ? 'LONG' : 'SHORT'} POS #${position.position_id} · SETUP #${position.setup_id}`
      const meta = { overlay_type: 'active_position', record_id: position.position_id }
      addPriceLine(position.entry, { color: '#38bdf8', lineWidth: 3, lineStyle: 0, axisLabelVisible: true, title: `${prefix} · ENTRY` }, { ...meta, field: 'entry' })
      addPriceLine(position.stop_loss, { color: '#dc2626', lineWidth: 3, lineStyle: 0, axisLabelVisible: true, title: `${prefix} · SL` }, { ...meta, field: 'stop_loss' })
      ;[position.take_profit_1, position.take_profit_2, position.take_profit_3].forEach((target, index) => addPriceLine(target, { color: '#16a34a', lineWidth: 2, lineStyle: 1, axisLabelVisible: true, title: `${prefix} · TP${index + 1}` }, { ...meta, field: `take_profit_${index + 1}` }))
    })

    chart.timeScale().fitContent()
    requestAnimationFrame(updateBands)
    return () => {
      chart.timeScale().unsubscribeVisibleLogicalRangeChange(rangeHandler)
      cleanup(bands)
    }
  }, [chartKey, candles, swings, structure, fvg, liquidity, orderBlocks, premiumDiscount, sweeps, setups, activePositions, waveCounts, settings, onLoadOlder])

  return <div className="chart" ref={host} />
}
