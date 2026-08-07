import { useEffect, useState } from 'react'
import { api } from '../api'
import { TIMEFRAMES } from '../constants'
import { formatDisplayValue, formatNumber } from '../format'

const Safety=()=> <div className="paper-banner"><strong>PAPER ANALYSIS ONLY</strong><span>NO LIVE ORDER EXECUTION · Setups express deterministic bias and confidence, never guaranteed profit.</span></div>
const Cell=({value,numeric=false})=><td>{numeric?formatNumber(value):formatDisplayValue(value)}</td>
const Field=({label,value})=><span>{label}<strong>{formatDisplayValue(value)}</strong></span>

export function LiquiditySweeps(){
  const [symbol,setSymbol]=useState('BTCUSDT'),[timeframe,setTimeframe]=useState('15m'),[rows,setRows]=useState([])
  useEffect(()=>{api.get('/liquidity-sweeps',{params:{symbol,timeframe,limit:500}}).then(r=>setRows(r.data))},[symbol,timeframe])
  return <><div className="page-head"><div><p className="eyebrow">LIQUIDITY ENGINE</p><h1>Liquidity Sweeps</h1></div><div className="controls"><select value={symbol} onChange={e=>setSymbol(e.target.value)}><option>BTCUSDT</option><option>ETHUSDT</option></select><select value={timeframe} onChange={e=>setTimeframe(e.target.value)}>{TIMEFRAMES.map(x=><option key={x}>{x}</option>)}</select></div></div><Safety/><section className="panel table-wrap"><table><thead><tr>{['Detected','Direction','Type','Liquidity','Extreme','Reclaim','Status','Confidence','Pool'].map(x=><th key={x}>{x}</th>)}</tr></thead><tbody>{rows.map(row=><tr key={row.id}><Cell value={row.detected_at}/><Cell value={row.direction}/><Cell value={row.sweep_type}/><Cell value={row.liquidity_price} numeric/><Cell value={row.extreme_price} numeric/><Cell value={row.reclaimed_price} numeric/><Cell value={row.status}/><Cell value={row.confidence_score} numeric/><Cell value={row.liquidity_pool_id}/></tr>)}</tbody></table>{!rows.length&&<p className="empty">No causal liquidity sweeps detected yet.</p>}</section></>
}

const SetupDrawer=({detail,close})=>{
  const s=detail.setup, wave=detail.elliott, sweep=detail.liquidity_sweep, pool=detail.liquidity_pool
  const [account,setAccount]=useState(null),[message,setMessage]=useState('')
  useEffect(()=>{api.get('/paper/accounts').then(r=>setAccount(r.data[0]))},[])
  const queue=async()=>{try{await api.post(`/trade-setups/${s.id}/paper-trade`,{account_id:account.id,max_risk_per_trade_pct:1,slippage_bps:2,taker_fee_pct:.05});setMessage('Queued for deterministic paper entry.')}catch(e){const detail=e.response?.data?.detail;setMessage(typeof detail==='string'?detail:detail?.message||e.message)}}
  return <div className="drawer-backdrop" onClick={close}><aside className="setup-drawer" onClick={e=>e.stopPropagation()}><button onClick={close}>Close ×</button><p className="eyebrow">PAPER ANALYSIS ONLY</p><h2>{s.strategy.replaceAll('_',' ')}</h2>
    <div className="drawer-grid"><Field label="Symbol" value={detail.symbol}/><Field label="Detected" value={s.detected_at}/><Field label="Direction" value={s.direction}/><Field label="Status" value={s.status}/><Field label="Setup timeframe" value={s.setup_timeframe}/><Field label="Entry timeframe" value={s.entry_timeframe}/><Field label="HTF" value={s.higher_timeframe}/><Field label="Structure" value={detail.structure?.event_type}/></div>
    <h3>Elliott context</h3><div className="drawer-grid"><Field label="Pattern" value={wave?.pattern_type}/><Field label="Wave" value={s.setup_conditions_json?.wave}/><Field label="Degree" value={wave?.degree}/><Field label="Direction" value={wave?.direction}/><Field label="Confidence" value={wave?.confidence_score}/><Field label="Invalidation" value={wave?.invalidation_price}/></div>
    <h3>Liquidity / zones</h3><div className="drawer-grid"><Field label="Latest sweep" value={sweep?.sweep_type}/><Field label="Liquidity level" value={pool?.price}/><Field label="Sweep confidence" value={sweep?.confidence_score}/><Field label="FVG zone" value={detail.fvg?`${detail.fvg.lower_price} – ${detail.fvg.upper_price}`:null}/><Field label="FVG status" value={detail.fvg?.status}/><Field label="Order block" value={detail.order_block?`${detail.order_block.bottom_price} – ${detail.order_block.top_price}`:null}/><Field label="Order block status" value={detail.order_block?.status}/></div>
    <h3>Execution geometry</h3><div className="drawer-grid"><Field label="Entry min" value={s.entry_min}/><Field label="Entry max" value={s.entry_max}/><Field label="Preferred entry" value={s.preferred_entry}/><Field label="Stop loss" value={s.stop_loss}/><Field label="TP1" value={s.take_profit_1}/><Field label="TP2" value={s.take_profit_2}/><Field label="TP3" value={s.take_profit_3}/><Field label="Risk reward" value={s.risk_reward_2}/><Field label="Confidence" value={s.confidence_score}/></div>
    <h3>Validation checklist</h3><div className="table-wrap"><table><thead><tr><th>Rule</th><th>Status</th><th>Actual</th><th>Required</th></tr></thead><tbody>{detail.validation.checklist.map(check=><tr key={check.rule}><td>{check.rule}</td><td><strong>{check.status}</strong></td><td>{formatDisplayValue(check.actual)}</td><td>{formatDisplayValue(check.required)}</td></tr>)}</tbody></table></div>
    <h3>Final status: {s.status.toUpperCase()}</h3>{detail.validation.rejection_reasons.length>0&&<><h3>Rejection reasons</h3><ul>{detail.validation.rejection_reasons.map(reason=><li key={reason}>{reason}</li>)}</ul></>}{s.status==='ready'&&account&&<button onClick={queue}>Queue paper trade · 1% risk</button>}{message&&<p>{message}</p>}<p className="safety-copy">No exchange order can be submitted from this screen.</p>
  </aside></div>
}

export function TradeSetups(){
  const [symbol,setSymbol]=useState('BTCUSDT'),[rows,setRows]=useState([]),[detail,setDetail]=useState(null)
  useEffect(()=>{api.get('/trade-setups',{params:{symbol,limit:500}}).then(r=>setRows(r.data))},[symbol])
  const select=async row=>setDetail((await api.get(`/trade-setups/${row.id}`)).data)
  return <><div className="page-head"><div><p className="eyebrow">DETERMINISTIC OPPORTUNITIES</p><h1>Trade Setups</h1></div><select value={symbol} onChange={e=>setSymbol(e.target.value)}><option>BTCUSDT</option><option>ETHUSDT</option></select></div><Safety/><section className="panel table-wrap"><table><thead><tr>{['Direction','Strategy','Status','Entry Zone','Preferred','SL','TP1','TP2','TP3','R:R 2','Confidence','Expires'].map(x=><th key={x}>{x}</th>)}</tr></thead><tbody>{rows.map(row=><tr key={row.id} onClick={()=>select(row)} className="clickable"><Cell value={row.direction}/><Cell value={row.strategy}/><Cell value={row.status}/><td>{formatNumber(row.entry_min)}–{formatNumber(row.entry_max)}</td><Cell value={row.preferred_entry} numeric/><Cell value={row.stop_loss} numeric/><Cell value={row.take_profit_1} numeric/><Cell value={row.take_profit_2} numeric/><Cell value={row.take_profit_3} numeric/><Cell value={row.risk_reward_2} numeric/><Cell value={row.confidence_score} numeric/><Cell value={row.expires_at}/></tr>)}</tbody></table>{!rows.length&&<p className="empty">No paper-analysis setups have been generated yet.</p>}</section>{detail&&<SetupDrawer detail={detail} close={()=>setDetail(null)}/>}</>
}
