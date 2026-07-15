import { useCallback, useEffect, useMemo, useState } from 'react'
import MarketChart from '../components/MarketChart'
import { api, getMarketBundle } from '../api'
import '../smc.css'

const Metric = ({ label, value }) => <div className="metric"><span>{label}</span><strong>{value ?? '—'}</strong></div>
const settingLabels = { majorSwings: 'Major Swings', internalSwings: 'Internal Swings', bos: 'BOS', choch: 'CHoCH', fvg: 'FVG', liquidity: 'Liquidity', orderBlocks: 'Order Blocks', sweeps: 'Liquidity Sweeps', setups: 'Trade Setups', entryZones: 'Entry Zones', targets: 'Targets', primaryWave: 'Primary Elliott Count', alternateWaves: 'Alternate Counts', fibonacci: 'Fibonacci Projections', waveTargets: 'Wave Targets' }

export default function MarketAnalysis() {
  const [symbol, setSymbol] = useState('BTCUSDT'), [timeframe, setTimeframe] = useState('1h')
  const [data, setData] = useState({ candles: [], swings: [], structure: [], fvg: [], liquidity: [], orderBlocks: [], sweeps: [], setups: [], waveCounts: [], analysis: null })
  const [chartSettings, setChartSettings] = useState({ majorSwings: true, internalSwings: true, bos: true, choch: true, fvg: true, liquidity: true, orderBlocks: true, sweeps: true, setups: true, entryZones: true, targets: true, primaryWave: true, alternateWaves: false, fibonacci: true, waveTargets: true })
  const [loading, setLoading] = useState(true), [error, setError] = useState(''), [connected, setConnected] = useState(false)
  const load = useCallback(async () => { setLoading(true); setError(''); try { setData(await getMarketBundle(symbol, timeframe)) } catch (e) { setError(e.response?.data?.detail || e.message) } finally { setLoading(false) } }, [symbol, timeframe])
  useEffect(() => { load() }, [load])
  useEffect(() => { api.get('/settings').then(({data}) => setChartSettings(current => ({...current, sweeps: data.chart_sweep_display, setups: data.chart_setup_display, entryZones: data.chart_setup_display, targets: data.chart_setup_display}))) }, [])
  useEffect(() => {
    const protocol = location.protocol === 'https:' ? 'wss:' : 'ws:'
    const socket = new WebSocket(`${protocol}//${location.host}/ws/market`)
    socket.onopen = () => setConnected(true); socket.onclose = () => setConnected(false)
    socket.onmessage = event => { const message = JSON.parse(event.data); const payload = message.data || {}; if (payload.symbol === symbol && (payload.timeframe === timeframe || payload.setup_timeframe === timeframe) && ['candle_closed','swing_point','bos','choch','fvg_new','fvg_mitigation','liquidity_new','liquidity_sweep_candidate','liquidity_sweep_confirmed','liquidity_sweep_invalidated','order_block_new','order_block_mitigation','elliott_wave_updated','trade_setup_created','trade_setup_ready','trade_setup_triggered','trade_setup_invalidated','trade_setup_expired','analysis_snapshot'].includes(message.type)) load() }
    const ping = setInterval(() => socket.readyState === WebSocket.OPEN && socket.send('ping'), 20000)
    return () => { clearInterval(ping); socket.close() }
  }, [symbol, timeframe, load])
  const indicators = data.analysis?.indicator_values_json || {}
  const chartCandles = useMemo(() => data.candles.map((c, index, all) => {
    const slice = all.slice(0, index + 1), closes = slice.map(x => +x.close)
    const ema = period => closes.length < period ? null : closes.reduce((previous, current, i) => i ? current * (2/(period+1)) + previous * (1-2/(period+1)) : current, closes[0])
    return { ...c, indicators: { ema20: ema(20) ? { time: Math.floor(new Date(c.open_time).getTime()/1000), value: ema(20) } : null, ema50: ema(50) ? { time: Math.floor(new Date(c.open_time).getTime()/1000), value: ema(50) } : null, ema200: ema(200) ? { time: Math.floor(new Date(c.open_time).getTime()/1000), value: ema(200) } : null } }
  }), [data.candles])
  const activeBlocks = data.orderBlocks.filter(x => ['active','partially_mitigated'].includes(x.status)).length
  const activeLiquidity = data.liquidity.filter(x => !x.swept_at).length
  return <div>
    <div className="page-head"><div><p className="eyebrow">LIVE SMC INTELLIGENCE</p><h1>Market Analysis</h1></div><div className="controls"><select value={symbol} onChange={e=>setSymbol(e.target.value)}><option>BTCUSDT</option><option>ETHUSDT</option></select><select value={timeframe} onChange={e=>setTimeframe(e.target.value)}><option>15m</option><option>1h</option><option>4h</option></select><span className={`connection ${connected?'online':''}`}><i />{connected?'Live':'Offline'}</span></div></div>
    {error && <div className="alert">{error}</div>}
    <div className="summary-grid"><Metric label="Market bias" value={data.bias?.label}/><Metric label="Structure score" value={data.score ? `${data.score.score} · ${data.score.label}` : null}/><Metric label="Current trend" value={data.analysis?.trend}/><Metric label="Latest structure" value={data.analysis?.latest_structure_event}/><Metric label="Active FVGs" value={data.analysis?.active_fvg_count}/><Metric label="Order blocks" value={activeBlocks}/><Metric label="Liquidity pools" value={activeLiquidity}/></div>
    <section className="panel chart-panel"><div className="panel-title"><span>{symbol} · {timeframe}</span><small>{loading ? 'Refreshing…' : `${data.candles.length} candles`}</small></div><div className="chart-settings">{Object.entries(settingLabels).map(([key,label])=><label key={key}><input type="checkbox" checked={chartSettings[key]} onChange={e=>setChartSettings({...chartSettings,[key]:e.target.checked})}/>{label}</label>)}</div><MarketChart candles={chartCandles} swings={data.swings} structure={data.structure} fvg={data.fvg} liquidity={data.liquidity} orderBlocks={data.orderBlocks} premiumDiscount={data.premiumDiscount} sweeps={data.sweeps} setups={data.setups} waveCounts={data.waveCounts} indicators={indicators} settings={chartSettings}/><div className="legend"><span className="ema20">EMA 20</span><span className="ema50">EMA 50</span><span>Elliott Primary / Alternates</span><span>Sweeps</span><span>Paper setups · Entry / SL / TP</span></div></section>
  </div>
}
