import { useEffect, useState } from 'react'
import { api } from '../api'
import { formatNumber } from '../format'

const Item = ({ label, value }) => <div className="wave-metric"><span>{label}</span><strong>{value ?? '—'}</strong></div>

export default function ElliottWave() {
  const [symbol, setSymbol] = useState('BTCUSDT')
  const [timeframe, setTimeframe] = useState('1h')
  const [rows, setRows] = useState([])
  const [selected, setSelected] = useState(null)
  useEffect(() => {
    api.get('/elliott-wave/counts', { params: { symbol, timeframe, limit: 100 } }).then(response => {
      setRows(response.data)
      setSelected(response.data.find(item => item.status === 'primary') || response.data[0] || null)
    })
  }, [symbol, timeframe])
  const primary = rows.find(item => item.status === 'primary')
  const alternates = rows.filter(item => item.status === 'alternate')
  return <>
    <div className="page-head"><div><p className="eyebrow">DETERMINISTIC FIBONACCI STRUCTURE</p><h1>Elliott Wave Analysis</h1></div><div className="controls"><select value={symbol} onChange={event => setSymbol(event.target.value)}><option>BTCUSDT</option><option>ETHUSDT</option></select><select value={timeframe} onChange={event => setTimeframe(event.target.value)}><option>15m</option><option>1h</option><option>4h</option></select></div></div>
    <div className="paper-banner"><strong>PAPER ANALYSIS ONLY</strong><span>Wave labels come from confirmed swings and deterministic Fibonacci rules—not LLM-generated predictions.</span></div>
    <div className="wave-summary"><Item label="Primary pattern" value={primary?.pattern_type?.replaceAll('_', ' ')}/><Item label="Current wave" value={primary?.metadata_json.current_wave}/><Item label="Degree" value={primary?.degree}/><Item label="Confidence" value={formatNumber(primary?.confidence_score)}/><Item label="Invalidation" value={formatNumber(primary?.invalidation_price)}/><Item label="Projected target" value={primary ? `${formatNumber(primary.projected_target_min)}–${formatNumber(primary.projected_target_max)}` : null}/><Item label="Alternates" value={alternates.length}/></div>
    <div className="wave-layout"><section className="panel table-wrap"><table><thead><tr>{['Rank', 'Status', 'Pattern', 'Degree', 'Wave', 'Direction', 'Confidence'].map(label => <th key={label}>{label}</th>)}</tr></thead><tbody>{rows.map(row => <tr key={row.id} className="clickable" onClick={() => setSelected(row)}><td>{row.rank || '—'}</td><td>{row.status}</td><td>{row.pattern_type}</td><td>{row.degree}</td><td>{row.metadata_json.current_wave}</td><td>{row.direction}</td><td>{formatNumber(row.confidence_score)}</td></tr>)}</tbody></table>{!rows.length && <p className="empty">No valid count has reached the confidence threshold yet.</p>}</section>
      {selected && <section className="panel wave-detail"><p className="eyebrow">{selected.status} COUNT</p><h2>{selected.pattern_type.replaceAll('_', ' ')}</h2><div className="wave-path">{selected.points.map(point => <span key={point.id}><b>{point.wave_label}</b>{formatNumber(point.price)}</span>)}</div><div className="wave-summary"><Item label="BOS/CHoCH" value={selected.structure_confirmation_json.event_id ? `#${selected.structure_confirmation_json.event_id}` : '—'}/><Item label="Liquidity sweep" value={selected.liquidity_confirmation_json.sweep_id ? `#${selected.liquidity_confirmation_json.sweep_id}` : '—'}/><Item label="FVG zones" value={selected.metadata_json.fvg_zone_ids?.join(', ') || '—'}/><Item label="Order blocks" value={selected.metadata_json.order_block_ids?.join(', ') || '—'}/></div><h3>Rules passed</h3><ul className="passed">{selected.rules_passed_json.map(rule => <li key={rule}>{rule.replaceAll('_', ' ')}</li>)}</ul>{selected.rules_failed_json.length > 0 && <><h3>Rules failed</h3><ul className="failed">{selected.rules_failed_json.map(rule => <li key={rule}>{rule.replaceAll('_', ' ')}</li>)}</ul></>}<h3>Fibonacci measurements</h3><pre>{JSON.stringify(selected.fibonacci_scores_json, null, 2)}</pre></section>}
    </div>
  </>
}
