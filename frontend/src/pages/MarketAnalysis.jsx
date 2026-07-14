import { useCallback, useEffect, useMemo, useState } from 'react'
import MarketChart from '../components/MarketChart'
import { getMarketBundle } from '../api'

const Metric = ({ label, value }) => <div className="metric"><span>{label}</span><strong>{value ?? '—'}</strong></div>

export default function MarketAnalysis() {
  const [symbol, setSymbol] = useState('BTCUSDT'), [timeframe, setTimeframe] = useState('1h')
  const [data, setData] = useState({ candles: [], swings: [], structure: [], fvg: [], analysis: null })
  const [loading, setLoading] = useState(true), [error, setError] = useState(''), [connected, setConnected] = useState(false)
  const load = useCallback(async () => { setLoading(true); setError(''); try { setData(await getMarketBundle(symbol, timeframe)) } catch (e) { setError(e.response?.data?.detail || e.message) } finally { setLoading(false) } }, [symbol, timeframe])
  useEffect(() => { load() }, [load])
  useEffect(() => {
    const protocol = location.protocol === 'https:' ? 'wss:' : 'ws:'
    const socket = new WebSocket(`${protocol}//${location.host}/ws/market`)
    socket.onopen = () => setConnected(true); socket.onclose = () => setConnected(false)
    socket.onmessage = event => { const message = JSON.parse(event.data); const payload = message.data || {}; if (payload.symbol === symbol && payload.timeframe === timeframe && ['candle_closed','swing_point','bos','choch','fvg_new','fvg_mitigation','analysis_snapshot'].includes(message.type)) load() }
    const ping = setInterval(() => socket.readyState === WebSocket.OPEN && socket.send('ping'), 20000)
    return () => { clearInterval(ping); socket.close() }
  }, [symbol, timeframe, load])
  const indicators = data.analysis?.indicator_values_json || {}
  const chartCandles = useMemo(() => data.candles.map((c, index, all) => {
    const slice = all.slice(0, index + 1), closes = slice.map(x => +x.close)
    const ema = period => closes.length < period ? null : closes.reduce((previous, current, i) => i ? current * (2/(period+1)) + previous * (1-2/(period+1)) : current, closes[0])
    return { ...c, indicators: { ema20: ema(20) ? { time: Math.floor(new Date(c.open_time).getTime()/1000), value: ema(20) } : null, ema50: ema(50) ? { time: Math.floor(new Date(c.open_time).getTime()/1000), value: ema(50) } : null, ema200: ema(200) ? { time: Math.floor(new Date(c.open_time).getTime()/1000), value: ema(200) } : null } }
  }), [data.candles])
  return <div>
    <div className="page-head"><div><p className="eyebrow">LIVE MARKET INTELLIGENCE</p><h1>Market Analysis</h1></div><div className="controls"><select value={symbol} onChange={e=>setSymbol(e.target.value)}><option>BTCUSDT</option><option>ETHUSDT</option></select><select value={timeframe} onChange={e=>setTimeframe(e.target.value)}><option>15m</option><option>1h</option><option>4h</option></select><span className={`connection ${connected?'online':''}`}><i />{connected?'Live':'Offline'}</span></div></div>
    {error && <div className="alert">{error}</div>}
    <div className="summary-grid"><Metric label="Current trend" value={data.analysis?.trend}/><Metric label="Latest structure" value={data.analysis?.latest_structure_event}/><Metric label="Active FVGs" value={data.analysis?.active_fvg_count}/><Metric label="RSI · 14" value={indicators.rsi14?.toFixed?.(2)}/><Metric label="MACD" value={indicators.macd?.toFixed?.(2)}/><Metric label="ATR · 14" value={indicators.atr14?.toFixed?.(2)}/><Metric label="Volume ratio" value={indicators.volume_ratio?.toFixed?.(2)}/></div>
    <section className="panel chart-panel"><div className="panel-title"><span>{symbol} · {timeframe}</span><small>{loading ? 'Refreshing…' : `${data.candles.length} candles`}</small></div><MarketChart candles={chartCandles} swings={data.swings} structure={data.structure} fvg={data.fvg} indicators={indicators}/><div className="legend"><span className="ema20">EMA 20</span><span className="ema50">EMA 50</span><span className="ema200">EMA 200</span><span>SH / SL Swings</span><span>BOS / CHoCH</span><span>Dashed · FVG bounds</span></div></section>
  </div>
}

