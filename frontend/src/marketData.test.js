import test from 'node:test'
import assert from 'node:assert/strict'
import { mergeLiveCandle, prependHistory, toChartCandle } from './marketData.js'

const candle=(time,close='100')=>({open_time:time,open:'99',high:'101',low:'98',close})

test('websocket update replaces the current candle without duplication',()=>{
  const rows=[candle('2026-01-01T00:00:00Z')]
  const next=mergeLiveCandle(rows,{...candle(rows[0].open_time,'105'),symbol:'BTCUSDT',timeframe:'1h'},'BTCUSDT','1h')
  assert.equal(next.length,1)
  assert.equal(next[0].close,'105')
})

test('a live update cannot mutate a closed candle',()=>{
  const closed={...candle('2026-01-01T00:00:00Z','100'),is_closed:true}
  const live={...closed,close:'999',is_closed:false,symbol:'BTCUSDT',timeframe:'1h'}
  assert.deepEqual(mergeLiveCandle([closed],live,'BTCUSDT','1h'),[closed])
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

test('chart OHLC mapping is exact and ignores analytical overlays',()=>{
  const row={open_time:'2026-08-19T00:00:00Z',open:'64000',high:'65000',low:'63000',close:'64500',stop_loss:'51000',take_profit_1:'70000'}
  assert.deepEqual(toChartCandle(row),{time:1787097600,open:64000,high:65000,low:63000,close:64500})
})
