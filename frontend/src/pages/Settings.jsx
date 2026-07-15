import { useEffect, useState } from 'react'
import { api } from '../api'

const NumberField = ({ label, name, form, set, step = .01, min = 0, max }) => <label>{label}<input type="number" step={step} min={min} max={max} value={form[name] ?? ''} onChange={event => set(name, +event.target.value)}/></label>
const Toggle = ({ label, name, form, set }) => <label className="toggle"><input type="checkbox" checked={form[name]} onChange={event => set(name, event.target.checked)}/><span/>{label}</label>

export default function Settings() {
  const [form, setForm] = useState(null)
  const [saved, setSaved] = useState(false)
  useEffect(() => { api.get('/settings').then(response => setForm(response.data)) }, [])
  if (!form) return <p>Loading settings…</p>
  const set = (key, value) => { setSaved(false); setForm({ ...form, [key]: value }) }
  const submit = event => { event.preventDefault(); api.put('/settings', form).then(response => { setForm(response.data); setSaved(true) }) }
  return <>
    <div className="page-head"><div><p className="eyebrow">CAUSAL ANALYSIS PARAMETERS</p><h1>Settings</h1></div></div>
    <form className="panel settings" onSubmit={submit}>
      <h3>Structure and zones</h3><div className="form-grid"><NumberField label="Swing left bars" name="swing_left_bars" form={form} set={set} step={1} min={1} max={20}/><NumberField label="Swing right bars" name="swing_right_bars" form={form} set={set} step={1} min={1} max={20}/><NumberField label="Minimum FVG · ATR" name="minimum_fvg_atr_size" form={form} set={set}/><NumberField label="Liquidity tolerance %" name="liquidity_tolerance_percentage" form={form} set={set}/></div>
      <h3>Liquidity sweeps</h3><div className="form-grid"><NumberField label="Minimum penetration %" name="sweep_minimum_penetration_percentage" form={form} set={set}/><NumberField label="Maximum penetration %" name="sweep_maximum_penetration_percentage" form={form} set={set}/><NumberField label="Confirmation candles" name="sweep_confirmation_candles" form={form} set={set} step={1}/><NumberField label="Minimum wick ratio" name="sweep_minimum_wick_ratio" form={form} set={set}/><NumberField label="Minimum sweep confidence" name="minimum_sweep_confidence" form={form} set={set} step={1} max={100}/><NumberField label="Sweep expiry candles" name="sweep_expiry_candles" form={form} set={set} step={1}/></div><Toggle label="Allow same-candle confirmation" name="sweep_allow_same_candle_confirmation" form={form} set={set}/>
      <h3>Elliott Wave engine</h3><div className="form-grid"><NumberField label="Fibonacci tolerance" name="elliott_fibonacci_tolerance" form={form} set={set}/><NumberField label="Maximum alternate counts" name="elliott_max_alternate_counts" form={form} set={set} step={1} max={5}/><NumberField label="Minimum wave confidence" name="elliott_minimum_confidence" form={form} set={set} step={1} max={100}/><NumberField label="Wave 5 risk factor" name="elliott_wave_5_risk_factor" form={form} set={set} max={1}/></div><Toggle label="Allow zigzag truncation" name="elliott_allow_zigzag_truncation" form={form} set={set}/>
      <h3>Paper setup generation</h3><div className="form-grid"><NumberField label="Minimum setup confidence" name="minimum_setup_confidence" form={form} set={set} step={1} max={100}/><NumberField label="Setup expiry candles" name="setup_expiry_candles" form={form} set={set} step={1}/><NumberField label="Stop-loss ATR buffer" name="stop_loss_atr_buffer" form={form} set={set}/><NumberField label="Minimum reward-to-risk" name="minimum_reward_to_risk" form={form} set={set}/><NumberField label="Counter-trend confidence" name="counter_trend_minimum_confidence" form={form} set={set} step={1} max={100}/></div><Toggle label="Enable counter-trend setups" name="counter_trend_setups_enabled" form={form} set={set}/><Toggle label="Show sweeps on chart" name="chart_sweep_display" form={form} set={set}/><Toggle label="Show setups on chart" name="chart_setup_display" form={form} set={set}/>
      <button type="submit">Save safe configuration</button>{saved && <em>Saved</em>}
    </form>
  </>
}
