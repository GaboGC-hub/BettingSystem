import { API_URL } from '../constants'

async function request(path, options = {}) {
  const res = await fetch(`${API_URL}${path}`, {
    headers: { 'Content-Type': 'application/json' },
    ...options,
  })
  if (!res.ok) {
    const text = await res.text().catch(() => '')
    throw new Error(`API ${res.status}: ${text || res.statusText}`)
  }
  const ct = res.headers.get('content-type') || ''
  return ct.includes('application/json') ? res.json() : res.text()
}

function getJSON(path) {
  return request(path, { method: 'GET' })
}

function postJSON(path, body) {
  return request(path, {
    method: 'POST',
    body: JSON.stringify(body),
  })
}

function del(path) {
  return request(path, { method: 'DELETE' })
}

// ── Matches ──
export const fetchMatches = () => getJSON('/matches')
export const addMatch = (url) => postJSON('/matches', { url })
export const removeMatch = (url) => del(`/matches?url=${encodeURIComponent(url)}`)
export const refreshMatch = (url) => postJSON('/matches/refresh', { url })
export const overrideMatch = (url, payload) => postJSON('/matches/override', { url, ...payload })
export const simulateMatch = (url, payload) => postJSON('/matches/simulate', { url, ...payload })
export const overrideStats = (url, stats) => postJSON('/matches/override_stats', { url, stats })
export const setScraperUrls = (urls) => postJSON('/matches/scraper', { urls })

// ── Bets ──
export const lockBet = (body) => postJSON('/bets/lock', body)
export const unlockBet = (lockId) => del(`/bets/lock/${lockId}`)

// ── Settings ──
export const fetchSettings = () => getJSON('/settings')
export const saveSettings = (settings) => postJSON('/settings', settings)

// ── History / PnL ──
export const fetchHistory = (limit = 200) => getJSON(`/history?limit=${limit}`)

// ── Control ──
export const fetchControl = () => getJSON('/control')
export const saveControl = (settings) => postJSON('/control', settings)
export const fetchTradeBlotter = (limit = 50) => getJSON(`/trade_blotter?limit=${limit}`)
