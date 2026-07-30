import test from 'node:test'
import assert from 'node:assert/strict'
import { mergeLiveCandle, prependHistory } from './marketData.js'

const candle=(time,close='100')=>({open_time:time,open:'99',high:'101',low:'98',close})

test('websocket update replaces the current candle without duplication',()=>{
  const rows=[candle('2026-01-01T00:00:00Z')]
  const next=mergeLiveCandle(rows,{...candle(rows[0].open_time,'105'),symbol:'BTCUSDT',timeframe:'1h'},'BTCUSDT','1h')
  assert.equal(next.length,1)
  assert.equal(next[0].close,'105')
})

test('timeframe changes never mix candle updates',()=>{
  const rows=[candle('2026-01-01T00:00:00Z')]
  assert.deepEqual(mergeLiveCandle(rows,{...candle('2026-01-01T01:00:00Z'),symbol:'BTCUSDT',timeframe:'15m'},'BTCUSDT','1h'),rows)
})

test('history pagination prepends chronologically and removes duplicates',()=>{
  const shared=candle('2026-01-02T00:00:00Z')
  const result=prependHistory([shared,candle('2026-01-03T00:00:00Z')],[candle('2026-01-01T00:00:00Z'),shared])
  assert.equal(result.length,3)
  assert.equal(result[0].open_time,'2026-01-01T00:00:00Z')
})
