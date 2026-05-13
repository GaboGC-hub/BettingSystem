import { useState, useEffect, useCallback } from 'react'

const API = 'http://localhost:8000/api'

/**
 * RecentBetsTable – tabla de apuestas recientes estilo dashboard de referencia.
 * Carga datos desde /api/history (endpoint de historial de bets).
 * Muestra: Partido, Mercado, Cuota, Stake, EV, P&L, Estado.
 */
export default function RecentBetsTable({ matches, settings }) {
  const [history, setHistory] = useState([])
  const [loading, setLoading] = useState(true)

  const loadHistory = useCallback(async () => {
    try {
      const res = await fetch(`${API}/history?limit=20`)
      if (res.ok) {
        const data = await res.json()
        setHistory(data.bets || data || [])
      } else {
        // fallback: construir desde matches en memoria
        setHistory(buildFromMatches(matches, settings))
      }
    } catch {
      setHistory(buildFromMatches(matches, settings))
    } finally {
      setLoading(false)
    }
  }, [matches, settings])

  useEffect(() => { loadHistory() }, [loadHistory])

  // Refrescar automáticamente cada 30s
  useEffect(() => {
    const t = setInterval(loadHistory, 30000)
    return () => clearInterval(t)
  }, [loadHistory])

  if (loading) return (
    <div className="dash-panel recent-bets-panel">
      <div className="dash-panel-header">
        <span className="dash-panel-title">Apuestas Recientes</span>
        <span className="tag tag-gold">Cargando…</span>
      </div>
      <div className="recent-bets-empty">Cargando historial…</div>
    </div>
  )

  return (
    <div className="dash-panel recent-bets-panel">
      <div className="dash-panel-header">
        <span className="dash-panel-title">Apuestas Recientes</span>
        <div style={{ display: 'flex', alignItems: 'center', gap: '6px' }}>
          <span className="tag tag-gold">Últimas {history.length}</span>
          <button
            className="ctrl-btn"
            onClick={loadHistory}
            style={{ padding: '2px 8px', fontSize: '10px' }}
          >↻</button>
        </div>
      </div>

      {history.length === 0 ? (
        <div className="recent-bets-empty">
          Sin historial todavía. Las apuestas aparecen aquí al liquidarse.
        </div>
      ) : (
        <div style={{ overflowX: 'auto' }}>
          <table className="bets-table">
            <thead>
              <tr>
                <th>Partido</th>
                <th>Mercado</th>
                <th>Cuota</th>
                <th>Stake</th>
                <th>EV</th>
                <th>P&amp;L</th>
                <th>Estado</th>
              </tr>
            </thead>
            <tbody>
              {history.map((bet, i) => {
                const isWon  = bet.resultado === 'win'
                const isLost = bet.resultado === 'loss'
                const isPend = !isWon && !isLost
                const statusClass = isWon ? 'st-won' : isLost ? 'st-lost' : 'st-pend'
                const statusLabel = isWon ? 'Ganada' : isLost ? 'Perdida' : 'Pendiente'
                const profitClass = isWon ? 'profit-pos' : isLost ? 'profit-neg' : 'profit-neu'
                const profitLabel = (bet.profit !== undefined && bet.profit !== null)
                  ? `${bet.profit >= 0 ? '+' : ''}$${bet.profit.toFixed(2)}`
                  : '—'
                const evVal = (bet.ev !== undefined && bet.ev !== null) ? bet.ev : null
                const evPct = evVal !== null ? ((evVal - 1) * 100) : null

                return (
                  <tr key={i}>
                    <td>
                      <div style={{ fontSize: '12px', fontWeight: 600, color: 'var(--text-main)', marginBottom: '2px' }}>
                        {bet.home_team || 'Desconocido'}
                      </div>
                      <div style={{ fontSize: '10px', color: 'var(--text-muted)' }}>
                        vs {bet.away_team || '—'}
                        {bet.minute ? ` · ${bet.minute}'` : ''}
                      </div>
                    </td>
                    <td style={{ color: 'var(--text-muted)', fontSize: '12px' }}>
                      {bet.market || '—'} — {bet.side || ''} {bet.linea !== undefined ? bet.linea : ''}
                    </td>
                    <td><span className="odds-val">{(bet.odds !== null && bet.odds !== undefined) ? bet.odds.toFixed(2) : '—'}</span></td>
                    <td style={{ fontFamily: 'var(--font-mono)', fontSize: '12px', color: 'var(--text-muted)' }}>
                      ${(bet.stake || 0).toFixed(2)}
                    </td>
                    <td>
                      {evPct !== null ? (
                        <div className="ev-bar">
                          <span style={{ fontSize: '11px', color: evPct >= 0 ? 'var(--accent-green)' : 'var(--accent-red)' }}>
                            {evPct >= 0 ? '+' : ''}{evPct.toFixed(1)}%
                          </span>
                          <div className="ev-track">
                            <div
                              className="ev-fill"
                              style={{
                                width: `${Math.min(100, Math.abs(evPct) * 10)}%`,
                                background: evPct >= 0 ? 'var(--accent-green)' : 'var(--accent-red)',
                              }}
                            />
                          </div>
                        </div>
                      ) : <span style={{ color: 'var(--text-dim)', fontSize: '11px' }}>—</span>}
                    </td>
                    <td><span className={profitClass}>{profitLabel}</span></td>
                    <td><span className={`status-badge ${statusClass}`}>{statusLabel}</span></td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}

// Construir historial desde datos en memoria (fallback cuando no hay endpoint)
function buildFromMatches(matches, settings) {
  const rows = []
  for (const d of Object.values(matches)) {
    const mkts = d?.result?.markets
    const snap = d?.snapshot
    if (!mkts || !snap) continue
    for (const mkt of ['goles', 'corners', 'tarjetas']) {
      const dec = mkts[mkt]?.decision
      if (!dec || dec.best_side === 'NO BET' || dec.best_side === 'PASAR') continue
      rows.push({
        home_team: snap.home_team || d?.match?.home_team || '—',
        away_team: snap.away_team || d?.match?.away_team || '—',
        minute: snap.state?.minuto ? Math.floor(snap.state.minuto) : null,
        market: mkt.charAt(0).toUpperCase() + mkt.slice(1),
        side: dec.best_side,
        linea: dec.linea,
        odds: dec.best_side === 'OVER' ? dec.raw_over : dec.raw_under,
        stake: (dec.best_stake || 0) * (settings?.bankroll || 1000),
        ev: dec.best_ev,
        profit: undefined,
        resultado: 'pending',
      })
    }
  }
  return rows
}
