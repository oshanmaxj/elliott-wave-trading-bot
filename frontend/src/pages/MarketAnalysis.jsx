import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import MarketChart from '../components/MarketChart'
import { api, getMarketBundle } from '../api'
import { mergeLiveCandle, prependHistory } from '../marketData'
import { TIMEFRAMES } from '../constants'
import '../smc.css'

const Metric=({label,value})=><div className="metric"><span>{label}</span><strong>{value??'—'}</strong></div>
const settingLabels={majorSwings:'Major Swings',internalSwings:'Internal Swings',bos:'BOS',choch:'CHoCH',fvg:'FVG',liquidity:'Liquidity',orderBlocks:'Order Blocks',sweeps:'Liquidity Sweeps',setups:'Trade Setups',entryZones:'Entry Zones',targets:'Targets',primaryWave:'Primary Elliott Count',alternateWaves:'Alternate Counts',fibonacci:'Fibonacci Projections',waveTargets:'Wave Targets'}
const cleanView={majorSwings:true,internalSwings:false,bos:true,choch:true,fvg:true,liquidity:false,orderBlocks:true,sweeps:true,setups:true,entryZones:true,targets:true,primaryWave:true,alternateWaves:false,fibonacci:false,waveTargets:true}
const rawView=Object.fromEntries(Object.keys(cleanView).map(key=>[key,false]))

export default function MarketAnalysis(){
  const [symbol,setSymbol]=useState('BTCUSDT'),[timeframe,setTimeframe]=useState('1h')
  const [data,setData]=useState({candles:[],swings:[],structure:[],fvg:[],liquidity:[],orderBlocks:[],sweeps:[],setups:[],waveCounts:[],analysis:null})
  const [chartSettings,setChartSettings]=useState(cleanView)
  const [loading,setLoading]=useState(true),[olderLoading,setOlderLoading]=useState(false),[error,setError]=useState('')
  const [connection,setConnection]=useState('RECONNECTING'),[lastUpdate,setLastUpdate]=useState(null),[coverage,setCoverage]=useState(null)
  const candlesRef=useRef([]),olderLoadingRef=useRef(false)
  candlesRef.current=data.candles
  const load=useCallback(async()=>{setLoading(true);setError('');try{setData(await getMarketBundle(symbol,timeframe))}catch(e){setError(e.response?.data?.detail||e.message)}finally{setLoading(false)}},[symbol,timeframe])
  useEffect(()=>{load()},[load])
  useEffect(()=>{api.get('/settings').then(({data})=>setChartSettings(current=>({...current,sweeps:data.chart_sweep_display,setups:data.chart_setup_display,entryZones:data.chart_setup_display,targets:data.chart_setup_display})))},[])
  useEffect(()=>{api.get('/market-data/coverage').then(r=>setCoverage(r.data?.[symbol]?.[timeframe]))},[symbol,timeframe])
  useEffect(()=>{
    let socket,reconnect,timer,stopped=false,lastMessage=Date.now()
    const connect=()=>{
      setConnection('RECONNECTING')
      const protocol=location.protocol==='https:'?'wss:':'ws:'
      socket=new WebSocket(`${protocol}//${location.host}/ws/market`)
      socket.onopen=async()=>{setConnection('LIVE');lastMessage=Date.now();try{const {data:state}=await api.get('/market/state',{params:{symbol,timeframe}});if(state.current_candle)setData(current=>({...current,candles:mergeLiveCandle(current.candles,state.current_candle,symbol,timeframe)}));if(state.last_event_at){lastMessage=new Date(state.last_event_at).getTime();setLastUpdate(new Date(state.last_event_at))}}catch{}}
      socket.onclose=()=>{setConnection('OFFLINE');if(!stopped)reconnect=setTimeout(connect,2000)}
      socket.onmessage=event=>{
        const message=JSON.parse(event.data),payload=message.data||{}
        lastMessage=Date.now();setLastUpdate(new Date())
        if(['candle_update','candle_closed'].includes(message.type)){
          setData(current=>({...current,candles:mergeLiveCandle(current.candles,payload,symbol,timeframe)}))
          if(message.type==='candle_closed')load()
        }else if(payload.symbol===symbol&&(payload.timeframe===timeframe||payload.setup_timeframe===timeframe))load()
      }
    }
    connect()
    timer=setInterval(async()=>{
      if(Date.now()-lastMessage>15000)setConnection('STALE')
      if(socket?.readyState===WebSocket.OPEN)socket.send('ping')
    },5000)
    return()=>{stopped=true;clearTimeout(reconnect);clearInterval(timer);socket?.close()}
  },[symbol,timeframe,load])
  const loadOlder=useCallback(async()=>{const first=candlesRef.current[0];if(!first||olderLoadingRef.current)return;olderLoadingRef.current=true;setOlderLoading(true);try{const {data:older}=await api.get('/candles',{params:{symbol,timeframe,limit:1000,before:first.open_time}});setData(current=>({...current,candles:prependHistory(current.candles,older)}))}finally{olderLoadingRef.current=false;setOlderLoading(false)}},[symbol,timeframe])
  const selectHistory=async days=>{setLoading(true);try{const after=new Date(Date.now()-days*86400000).toISOString();let response=await api.get('/candles',{params:{symbol,timeframe,limit:1500,after}}),rows=response.data;while(rows.length<3000&&rows.length&&new Date(rows[0].open_time)>new Date(after)&&response.data.length===1500){response=await api.get('/candles',{params:{symbol,timeframe,limit:Math.min(1500,3000-rows.length),after,before:rows[0].open_time}});rows=prependHistory(rows,response.data)}setData(current=>({...current,candles:rows.slice(-3000)}))}finally{setLoading(false)}}
  const indicators=data.analysis?.indicator_values_json||{}
  const chartCandles=useMemo(()=>data.candles.map((c,index,all)=>{const closes=all.slice(0,index+1).map(x=>+x.close),ema=period=>closes.length<period?null:closes.reduce((previous,current,i)=>i?current*(2/(period+1))+previous*(1-2/(period+1)):current,closes[0]);return {...c,indicators:{ema20:ema(20)?{time:Math.floor(new Date(c.open_time).getTime()/1000),value:ema(20)}:null,ema50:ema(50)?{time:Math.floor(new Date(c.open_time).getTime()/1000),value:ema(50)}:null,ema200:ema(200)?{time:Math.floor(new Date(c.open_time).getTime()/1000),value:ema(200)}:null}}}),[data.candles])
  const activeBlocks=data.orderBlocks.filter(x=>['active','partially_mitigated'].includes(x.status)).length,activeLiquidity=data.liquidity.filter(x=>!x.swept_at).length
  return <div>
    <div className="page-head"><div><p className="eyebrow">LIVE SMC INTELLIGENCE</p><h1>Market Analysis</h1><p className="subhead">Data coverage: {coverage?.available_from?`${new Date(coverage.available_from).toLocaleDateString()} → ${new Date(coverage.available_to).toLocaleDateString()}`:'checking…'} · Last update: {lastUpdate?`${Math.max(0,Math.floor((Date.now()-lastUpdate)/1000))} sec ago`:'waiting'}</p></div><div className="controls"><select value={symbol} onChange={e=>setSymbol(e.target.value)}><option>BTCUSDT</option><option>ETHUSDT</option></select><select value={timeframe} onChange={e=>setTimeframe(e.target.value)}>{TIMEFRAMES.map(x=><option key={x}>{x}</option>)}</select><span className={`connection ${connection==='LIVE'?'online':''}`}><i/>{connection}</span></div></div>
    {error&&<div className="alert">{String(error)}</div>}
    <div className="summary-grid"><Metric label="Market bias" value={data.bias?.label}/><Metric label="Structure score" value={data.score?`${data.score.score} · ${data.score.label}`:null}/><Metric label="Current trend" value={data.analysis?.trend}/><Metric label="Latest structure" value={data.analysis?.latest_structure_event}/><Metric label="Active FVGs" value={data.analysis?.active_fvg_count}/><Metric label="Order blocks" value={activeBlocks}/><Metric label="Liquidity pools" value={activeLiquidity}/></div>
    <section className="panel chart-panel"><div className="panel-title"><span>BINANCE · {symbol} · {timeframe}</span><div>{[['1D',1],['7D',7],['30D',30],['90D',90],['180D',180],['1Y',365]].map(([label,days])=><button key={label} onClick={()=>selectHistory(days)}>{label}</button>)}<button className="clean-button" onClick={()=>setChartSettings(rawView)}>Raw Candles</button><button className="clean-button" onClick={()=>setChartSettings(cleanView)}>Clean View</button><small>{loading?'Refreshing…':olderLoading?'Loading older history…':`${data.candles.length} candles`}</small></div></div><div className="chart-settings">{Object.entries(settingLabels).map(([key,label])=><label key={key}><input type="checkbox" checked={chartSettings[key]} onChange={e=>setChartSettings({...chartSettings,[key]:e.target.checked})}/>{label}</label>)}</div><MarketChart candles={chartCandles} swings={data.swings} structure={data.structure} fvg={data.fvg} liquidity={data.liquidity} orderBlocks={data.orderBlocks} premiumDiscount={data.premiumDiscount} sweeps={data.sweeps} setups={data.setups} waveCounts={data.waveCounts} indicators={indicators} settings={chartSettings} onLoadOlder={loadOlder}/><div className="legend"><span className="ema20">EMA 20</span><span className="ema50">EMA 50</span><span>Elliott Primary / Alternates</span><span>Sweeps</span><span>Paper setups · Entry / SL / TP</span></div></section>
  </div>
}
