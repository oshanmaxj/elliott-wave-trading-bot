import { useEffect, useState } from 'react'
import { Activity, Bell, Crosshair, Layers3, Radio, ShieldCheck, Target, TrendingUp, Waves } from 'lucide-react'
import { api } from '../api'
import { formatNumber } from '../format'

const Card = ({ icon: Icon, label, value, tone = '' }) => <div className={`status-card ${tone}`}><Icon/><span>{label}</span><strong>{value ?? '—'}</strong></div>
const waveText = wave => wave ? `${wave.direction} ${wave.pattern_type.replaceAll('_', ' ')} · ${wave.current_wave}` : '—'

export default function Dashboard({ navigate }) {
  const [health, setHealth] = useState(null)
  const [symbol, setSymbol] = useState('BTCUSDT')
  const [data, setData] = useState({})
  const [diagnostics, setDiagnostics] = useState(null)
  useEffect(() => { api.get('/health').then(response => setHealth(response.data)) }, [])
  useEffect(() => { api.get('/strategy-diagnostics', { params: { period: '24h' } }).then(response => setDiagnostics(response.data)).catch(() => setDiagnostics(null)) }, [])
  useEffect(() => {
    const params = { symbol, timeframe: '1h' }
    const safe = request => request.catch(() => ({ data: null }))
    Promise.all([
      safe(api.get('/market-bias', { params: { symbol } })), safe(api.get('/structure-score', { params })),
      api.get('/structure', { params }), api.get('/fvg', { params }), api.get('/order-blocks', { params }),
      api.get('/liquidity', { params }), api.get('/alerts', { params: { symbol, limit: 5 } }),
      api.get('/candles', { params: { ...params, limit: 1 } }),
      api.get('/liquidity-sweeps', { params: { ...params, status: 'confirmed', limit: 1 } }),
      api.get('/trade-setups/summary', { params: { symbol } }), api.get('/trade-setups', { params: { symbol, limit: 100 } }),
      safe(api.get('/elliott-wave/latest', { params })), safe(api.get('/elliott-wave/context', { params: { symbol } })),
      safe(api.get('/elliott-wave/counts', { params: { ...params, status: 'alternate', limit: 5 } })),
      safe(api.get('/execution/positions')),
    ]).then(([bias, score, structure, fvg, blocks, liquidity, alerts, candles, sweeps, summary, setups, wave, waveContext, alternateWaves, positions]) => setData({
      bias: bias.data, score: score.data, structure: structure.data, fvg: fvg.data, blocks: blocks.data,
      liquidity: liquidity.data, alerts: alerts.data, lastPrice: +candles.data.at(-1)?.close,
      latestSweep: sweeps.data.at(-1), summary: summary.data, setups: setups.data, wave: wave.data, waveContext: waveContext.data, alternateWaves: alternateWaves.data || [], positions: positions.data || [],
    }))
  }, [symbol])

  const latest = type => [...(data.structure || [])].reverse().find(item => item.event_type === type)
  const activeFvg = (data.fvg || []).filter(item => ['active', 'partially_mitigated'].includes(item.status)).length
  const activeBlocks = (data.blocks || []).filter(item => ['active', 'partially_mitigated'].includes(item.status)).length
  const nearest = (data.liquidity || []).filter(item => item.status === 'active').sort((left, right) => Math.abs(+left.price-data.lastPrice)-Math.abs(+right.price-data.lastPrice))[0]
  const opportunity = [...(data.setups || [])].filter(item => ['ready', 'triggered'].includes(item.status)).sort((left, right) => +right.confidence_score-+left.confidence_score)[0]
  const tone = data.score?.score >= 70 ? 'bullish' : data.score?.score < 50 ? 'bearish' : 'neutral'
  const livePnl = (data.positions || []).filter(position => position.status !== 'closed').every(position => position.unrealized_pnl !== null) ? (data.positions || []).filter(position => position.status !== 'closed').reduce((sum, position) => sum + Number(position.unrealized_pnl), 0).toFixed(2) : null
  return <>
    <div className="smc-cards"><Card icon={TrendingUp} label="Live unrealized PnL" value={livePnl === null ? 'Unavailable' : `$${livePnl}`}/></div>
    <div className="page-head"><div><p className="eyebrow">PAPER ANALYSIS · SMC + ELLIOTT WAVE</p><h1>Command Center</h1><p className="subhead">Deterministic setup, bias and wave-count context. No live execution.</p></div><select value={symbol} onChange={event => setSymbol(event.target.value)}><option>BTCUSDT</option><option>ETHUSDT</option></select></div>
    <div className="hero-grid"><section className={`panel hero bias-${tone}`}><span className="kicker">MARKET BIAS · {symbol}</span><h2>{data.bias?.label || 'Building context…'}</h2><p className="bias-detail">H4 {data.bias?.timeframes?.['4h'] || '—'} · H1 {data.bias?.timeframes?.['1h'] || '—'} · M15 {data.bias?.timeframes?.['15m'] || '—'}</p><button onClick={() => navigate('analysis')}>Open paper analysis →</button></section><div className="status-stack"><Card icon={TrendingUp} label="Structure score" value={data.score ? `${data.score.score} · ${data.score.label}` : null} tone={tone}/><Card icon={Radio} label="Market stream" value={health?.market_stream?.connected ? 'live' : 'reconnecting'}/><Card icon={ShieldCheck} label="Paper analysis" value={health?.status || 'checking'}/><Card icon={Bell} label="Recent alerts" value={data.alerts?.length}/></div></div>
    <div className="smc-cards"><Card icon={Waves} label="Current pattern" value={data.wave?.pattern_type?.replaceAll('_', ' ')}/><Card icon={Waves} label="Current wave" value={data.wave?.metadata_json?.current_wave}/><Card icon={Waves} label="Degree" value={data.wave?.degree}/><Card icon={TrendingUp} label="Wave confidence" value={data.wave && formatNumber(data.wave.confidence_score)}/><Card icon={Crosshair} label="Invalidation" value={data.wave && formatNumber(data.wave.invalidation_price)}/><Card icon={Target} label="Projected target" value={data.wave && `${formatNumber(data.wave.projected_target_min)}–${formatNumber(data.wave.projected_target_max)}`}/><Card icon={Waves} label="Alternate counts" value={data.alternateWaves?.length}/></div>
    {data.waveContext && <section className="panel safety"><p className="eyebrow">MULTI-TIMEFRAME ELLIOTT CONTEXT</p><p>4H {waveText(data.waveContext.timeframes?.['4h'])}</p><p>1H {waveText(data.waveContext.timeframes?.['1h'])}</p><p>15M {waveText(data.waveContext.timeframes?.['15m'])}</p></section>}
    <div className="smc-cards"><Card icon={Activity} label="Latest sweep" value={data.latestSweep?.sweep_type}/><Card icon={Target} label="Ready setups" value={data.summary?.ready_count}/><Card icon={TrendingUp} label="Highest confidence" value={opportunity ? formatNumber(opportunity.confidence_score) : null}/><Card icon={Crosshair} label="Bullish setups" value={data.summary?.bullish_count}/><Card icon={Crosshair} label="Bearish setups" value={data.summary?.bearish_count}/></div>
    <section className={`panel safety ${diagnostics?.closed_candles_processed === 0 ? 'bearish' : ''}`}><p className="eyebrow">STRATEGY PIPELINE · LAST 24 HOURS</p>{diagnostics ? <><div className="smc-cards">{[['Closed candles', diagnostics.closed_candles_processed], ['Strategy evaluations', diagnostics.strategy_evaluations], ['Candidates generated', diagnostics.candidates_generated], ['Candidates rejected', diagnostics.candidates_rejected], ['Setups persisted', diagnostics.setups_persisted], ['Execution eligible', diagnostics.execution_eligible], ['Auto execution started', diagnostics.auto_execution_started], ['Orders created', diagnostics.orders_created], ['Orders submitted', diagnostics.orders_submitted], ['Orders filled', diagnostics.orders_filled], ['Rejected / failed', diagnostics.orders_rejected_failed]].map(([label, value]) => <Card key={label} icon={Activity} label={label} value={value}/>)}</div><div className="smc-cards">{[['Swing points', 'swing_points_created'], ['Structure events', 'structure_events_created'], ['FVGs', 'fvg_zones_created'], ['Order blocks', 'order_blocks_created'], ['Liquidity sweeps', 'liquidity_sweeps_created'], ['Trade setups', 'trade_setups_created']].map(([label, key]) => <Card key={key} icon={Layers3} label={label} value={diagnostics.analysis_activity?.[key] ?? 0}/>)}</div>{diagnostics.closed_candles_processed === 0 && <p>Pipeline problem: the bot has not processed any closed candles in this period.</p>}<p>Main no-candidate reasons: {Object.entries(diagnostics.no_candidate_reasons || {}).sort((a,b) => b[1]-a[1]).slice(0, 6).map(([reason, count]) => `${reason.replaceAll('_', ' ')} (${count})`).join(' · ') || 'None'}</p><p>Main setup rejection reasons: {Object.entries(diagnostics.rejection_reasons || {}).sort((a,b) => b[1]-a[1]).slice(0, 5).map(([reason, count]) => `${reason.replaceAll('_', ' ')} (${count})`).join(' · ') || 'None'}</p></> : <p>Diagnostics unavailable.</p>}</section>
    {opportunity && <section className="panel opportunity"><p className="eyebrow">CURRENT OPPORTUNITY · PAPER ANALYSIS ONLY</p><h2>{symbol} {opportunity.setup_timeframe} · {opportunity.strategy.replaceAll('_', ' ')}</h2><div className="opportunity-grid">{[['Confidence', opportunity.confidence_score], ['Entry', `${formatNumber(opportunity.entry_min)}–${formatNumber(opportunity.entry_max)}`], ['SL', opportunity.stop_loss], ['TP1', opportunity.take_profit_1], ['TP2', opportunity.take_profit_2], ['TP3', opportunity.take_profit_3], ['R:R to TP2', opportunity.risk_reward_2]].map(([label, value]) => <span key={label}>{label}<strong>{typeof value === 'string' ? value : formatNumber(value)}</strong></span>)}</div>{opportunity.elliott_wave_count_id && <p>Wave count #{opportunity.elliott_wave_count_id} supplies the deterministic target and invalidation context.</p>}<p>NO LIVE ORDER EXECUTION · Confidence is not a profit guarantee.</p></section>}
    <div className="smc-cards"><Card icon={Activity} label="Latest BOS" value={latest('BOS')?.direction}/><Card icon={Crosshair} label="Latest CHoCH" value={latest('CHoCH')?.direction}/><Card icon={Layers3} label="Active FVG" value={activeFvg}/><Card icon={Target} label="Active order blocks" value={activeBlocks}/><Card icon={Crosshair} label="Nearest liquidity" value={nearest ? `${nearest.type} · ${nearest.price}` : null}/></div>
    <section className="panel safety"><p className="eyebrow">LATEST ALERTS</p>{data.alerts?.length ? data.alerts.map(alert => <p key={alert.id}>{alert.type} · {alert.message}</p>) : <p>No setup alerts yet.</p>}</section>
  </>
}
