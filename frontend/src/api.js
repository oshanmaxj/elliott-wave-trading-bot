import axios from 'axios'

export const api = axios.create({ baseURL: '/api', timeout: 30000, withCredentials: true })
export const readCookie = (name, source = typeof document === 'undefined' ? '' : document.cookie) => source.split('; ').find(x => x.startsWith(`${name}=`))?.split('=').slice(1).join('=')
export const applyCsrf = (config, source) => {
  if (!['get','head','options'].includes(config.method?.toLowerCase())) {
    const csrf = readCookie('wavescope_csrf', source)
    if (csrf) config.headers['X-CSRF-Token'] = decodeURIComponent(csrf)
  }
  return config
}
api.interceptors.request.use(config => applyCsrf(config))
api.interceptors.response.use(x=>x,async error=>{const cfg=error.config||{};if(cfg.method==='get'&&(!error.response||error.response.status>=500)&&(cfg.__retries||0)<2){cfg.__retries=(cfg.__retries||0)+1;await new Promise(r=>setTimeout(r,250*2**cfg.__retries));return api(cfg)}return Promise.reject(error)})
export const saveBinanceCredentials = payload => api.post('/binance/credentials', payload, { timeout: 15000 })
export const uniqueById = rows => [...new Map((rows || []).map(row => [row.id ?? `${row.source_table}:${row.source_record_id}`, row])).values()]
export const getMarketBundle = async (symbol, timeframe) => {
  const params = { symbol, timeframe }
  const safe = promise => promise.catch(error => error.response?.status === 404 ? { data: null } : Promise.reject(error))
  const [candles, swings, structure, fvg, analysis, liquidity, orderBlocks, premiumDiscount, bias, score, sweeps, setups, waveCounts, waveContext, activePositions] = await Promise.all([
    api.get('/candles', { params: { ...params, limit: 1000 } }),
    api.get('/swings', { params }), api.get('/structure', { params }), api.get('/fvg', { params }),
    safe(api.get('/analysis/latest', { params })),
    api.get('/liquidity', { params }), api.get('/order-blocks', { params }),
    safe(api.get('/premium-discount', { params })), safe(api.get('/market-bias', { params: { symbol } })),
    safe(api.get('/structure-score', { params })),
    api.get('/liquidity-sweeps', { params }), api.get('/trade-setups', { params }),
    api.get('/elliott-wave/counts', { params: { ...params, limit: 20 } }), api.get('/elliott-wave/context', { params: { symbol } }),
    api.get('/execution/position-overlays', { params: { symbol } }),
  ])
  return { candles: uniqueById(candles.data), swings: uniqueById(swings.data), structure: uniqueById(structure.data), fvg: uniqueById(fvg.data), analysis: analysis.data, liquidity: uniqueById(liquidity.data), orderBlocks: uniqueById(orderBlocks.data), premiumDiscount: premiumDiscount.data, bias: bias.data, score: score.data, sweeps: uniqueById(sweeps.data), setups: uniqueById(setups.data.filter(x => x.setup_timeframe === timeframe)), waveCounts: uniqueById(waveCounts.data), waveContext: waveContext.data, activePositions: uniqueById(activePositions.data) }
}
