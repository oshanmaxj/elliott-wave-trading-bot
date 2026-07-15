import axios from 'axios'

export const api = axios.create({ baseURL: '/api', timeout: 30000 })
export const getMarketBundle = async (symbol, timeframe) => {
  const params = { symbol, timeframe }
  const safe = promise => promise.catch(error => error.response?.status === 404 ? { data: null } : Promise.reject(error))
  const [candles, swings, structure, fvg, analysis, liquidity, orderBlocks, premiumDiscount, bias, score, sweeps, setups, waveCounts, waveContext] = await Promise.all([
    api.get('/candles', { params: { ...params, limit: 1000 } }),
    api.get('/swings', { params }), api.get('/structure', { params }), api.get('/fvg', { params }),
    safe(api.get('/analysis/latest', { params })),
    api.get('/liquidity', { params }), api.get('/order-blocks', { params }),
    safe(api.get('/premium-discount', { params })), safe(api.get('/market-bias', { params: { symbol } })),
    safe(api.get('/structure-score', { params })),
    api.get('/liquidity-sweeps', { params }), api.get('/trade-setups', { params: { symbol } }),
    api.get('/elliott-wave/counts', { params: { ...params, limit: 20 } }), api.get('/elliott-wave/context', { params: { symbol } }),
  ])
  return { candles: candles.data, swings: swings.data, structure: structure.data, fvg: fvg.data, analysis: analysis.data, liquidity: liquidity.data, orderBlocks: orderBlocks.data, premiumDiscount: premiumDiscount.data, bias: bias.data, score: score.data, sweeps: sweeps.data, setups: setups.data.filter(x => x.setup_timeframe === timeframe), waveCounts: waveCounts.data, waveContext: waveContext.data }
}
