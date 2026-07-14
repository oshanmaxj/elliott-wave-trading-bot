import axios from 'axios'

export const api = axios.create({ baseURL: '/api', timeout: 30000 })
export const getMarketBundle = async (symbol, timeframe) => {
  const params = { symbol, timeframe }
  const safe = promise => promise.catch(error => error.response?.status === 404 ? { data: null } : Promise.reject(error))
  const [candles, swings, structure, fvg, analysis] = await Promise.all([
    api.get('/candles', { params: { ...params, limit: 1000 } }),
    api.get('/swings', { params }), api.get('/structure', { params }), api.get('/fvg', { params }),
    safe(api.get('/analysis/latest', { params })),
  ])
  return { candles: candles.data, swings: swings.data, structure: structure.data, fvg: fvg.data, analysis: analysis.data }
}

