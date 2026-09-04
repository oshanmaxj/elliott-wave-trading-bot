import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'

const source = readFileSync(new URL('./pages/ExecutionPages.jsx', import.meta.url), 'utf8')

test('risk settings are split into per-trade, portfolio, loss and take-profit groups', () => {
  assert.match(source, /Per-trade risk/)
  assert.match(source, /Portfolio limits/)
  assert.match(source, /Loss limits/)
  assert.match(source, /Take-profit split/)
})

test('unenforced loss-limit fields are visibly flagged rather than silently accepted', () => {
  assert.match(source, /weekly_loss_pct.*true/)
  assert.match(source, /max_drawdown_pct.*true/)
  assert.match(source, /not yet enforced/)
})

test('take-profit split shows a running total that flags an invalid sum', () => {
  assert.match(source, /tpTotal=TP_FIELDS\.reduce/)
  assert.match(source, /tpValid=Math\.abs\(tpTotal-100\)<=0\.5/)
  assert.match(source, /must sum to 100%/)
})

test('save is blocked while any field fails validation', () => {
  assert.match(source, /hasErrors=Object\.values\(errors\)\.some\(Boolean\)\|\|!tpValid/)
  assert.match(source, /disabled=\{hasErrors\|\|busy\}/)
})

test('save shows a busy label while in flight and a success flash on completion', () => {
  assert.match(source, /busy\?'Saving…':saved\?'Saved ✓':'Save Risk Settings'/)
  assert.match(source, /className=\{`primary \$\{saved\?'success-flash':''\}`\}/)
  assert.match(source, /setSaved\(true\);setTimeout\(\(\)=>setSaved\(false\),1500\)/)
})

test('the dollar-risk preview reuses the real Binance balances endpoint', () => {
  assert.match(source, /useLoad\('\/binance\/balances',\[\]\)/)
  assert.match(source, /bal\.data\.find\(x=>x\.asset==='USDT'\)/)
})
