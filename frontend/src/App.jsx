import { useState } from 'react'
import { Activity, BarChart3, BookOpen, CircleGauge, Droplets, Layers3, ScrollText, Settings as SettingsIcon, Target, Waves } from 'lucide-react'
import Dashboard from './pages/Dashboard'
import MarketAnalysis from './pages/MarketAnalysis'
import { FVGZones, StructureEvents, SystemLogs } from './pages/DataPages'
import Settings from './pages/Settings'
import { LiquiditySweeps, TradeSetups } from './pages/OpportunityPages'
import ElliottWave from './pages/ElliottWave'

const nav=[['dashboard','Overview',CircleGauge],['analysis','Market Analysis',BarChart3],['elliott','Elliott Wave',Waves],['sweeps','Liquidity Sweeps',Droplets],['setups','Trade Setups',Target],['structure','Structure Events',Activity],['fvg','FVG Zones',Layers3],['logs','System Logs',ScrollText],['settings','Settings',SettingsIcon]]
export default function App(){const [page,setPage]=useState('dashboard');const pages={dashboard:<Dashboard navigate={setPage}/>,analysis:<MarketAnalysis/>,elliott:<ElliottWave/>,sweeps:<LiquiditySweeps/>,setups:<TradeSetups/>,structure:<StructureEvents/>,fvg:<FVGZones/>,logs:<SystemLogs/>,settings:<Settings/>};return <div className="shell"><aside><div className="brand"><div className="mark"><BookOpen size={19}/></div><div><strong>WaveScope</strong><small>MARKET INTELLIGENCE</small></div></div><nav>{nav.map(([id,label,Icon])=><button key={id} className={page===id?'active':''} onClick={()=>setPage(id)}><Icon size={17}/>{label}</button>)}</nav><div className="aside-foot"><span><i/> ANALYSIS ONLINE</span><small>Paper analysis · No live orders</small></div></aside><main>{pages[page]}</main></div>}
