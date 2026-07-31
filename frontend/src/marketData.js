export const normalizeLiveCandle = payload => ({
  ...payload,
  id: payload.id ?? `live-${payload.symbol}-${payload.timeframe}-${payload.open_time}`,
})

export const mergeLiveCandle = (candles, payload, symbol, timeframe) => {
  if (payload.symbol !== symbol || payload.timeframe !== timeframe) return candles
  const next = normalizeLiveCandle(payload)
  const index = candles.findIndex(item => item.open_time === next.open_time)
  if (index < 0) return [...candles, next].sort((a,b)=>new Date(a.open_time)-new Date(b.open_time))
  return candles.map((item, i) => i === index ? (item.is_closed && !next.is_closed ? item : {...item, ...next}) : item)
}

export const prependHistory = (current, older) => {
  const byTime = new Map([...older, ...current].map(item => [item.open_time, item]))
  return [...byTime.values()].sort((a,b)=>new Date(a.open_time)-new Date(b.open_time))
}
