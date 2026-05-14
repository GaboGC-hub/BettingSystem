import { useState, useEffect, useCallback } from 'react'
import { fetchControl, fetchTradeBlotter, saveControl } from '../services/api'
import { TRADE_BLOTTER_POLL_INTERVAL_MS } from '../constants'

export default function CommandCenter() {
  const [control, setControl] = useState({
    bot_status: 'RUNNING',
    min_ev_threshold: 0.08,
    max_open_bets_per_match: 2,
  })
  const [blotter, setBlotter] = useState([])
  const [loading, setLoading] = useState(false)
  const [statusMsg, setStatusMsg] = useState('')

  const loadControl = useCallback(async () => {
    try {
      setControl(await fetchControl())
    } catch {}
  }, [])

  const loadBlotter = useCallback(async () => {
    try {
      const data = await fetchTradeBlotter(50)
      setBlotter(data.blotter || [])
    } catch {}
  }, [])

  useEffect(() => {
    loadControl()
    loadBlotter()
    const interval = setInterval(() => { loadControl(); loadBlotter() }, TRADE_BLOTTER_POLL_INTERVAL_MS)
    return () => clearInterval(interval)
  }, [loadControl, loadBlotter])

  const updateControl = async (patch) => {
    setLoading(true)
    setStatusMsg('')
    try {
      const data = await saveControl(patch)
      setControl(data.control)
      setStatusMsg('OK')
    } catch {
      setStatusMsg('Error de conexión')
    }
    setLoading(false)
    setTimeout(() => setStatusMsg(''), 1500)
  }

  const isPaused = control.bot_status === 'PAUSED'
  const executedBets = blotter.filter(b => b.status === 'EXECUTED')
  const blockedBets = blotter.filter(b => b.status && b.status.startsWith('BLOCKED'))

  return (
    <div style={{
      background: 'var(--bg-card, #13161E)',
      border: '1px solid var(--border, rgba(201,168,76,0.18))',
      borderRadius: '12px',
      padding: '1.25rem',
      marginBottom: '1rem',
      flexShrink: 0,
      overflowY: 'auto',
      maxHeight: '40vh',
    }}>
      <div style={{
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'space-between',
        marginBottom: '1rem',
      }}>
        <div>
          <h2 style={{
            fontFamily: 'var(--font-display)',
            fontSize: '1.1rem',
            fontWeight: 700,
            color: 'var(--text-main)',
            margin: 0,
          }}>
            🎛 Command Center
          </h2>
          <p style={{
            fontFamily: 'var(--font-mono)',
            fontSize: '0.75rem',
            color: 'var(--text-muted)',
            margin: '0.2rem 0 0',
          }}>
            Control dinámico sin reinicio
          </p>
        </div>
        {statusMsg && (
          <span style={{
            fontFamily: 'var(--font-mono)',
            fontSize: '0.7rem',
            color: statusMsg === 'OK' ? 'var(--accent-green)' : 'var(--accent-red)',
            background: 'var(--bg-panel)',
            padding: '0.25rem 0.6rem',
            borderRadius: '6px',
          }}>
            {statusMsg}
          </span>
        )}
      </div>

      {/* ── Kill Switch & Exposure Limit ── */}
      <div style={{
        display: 'grid',
        gridTemplateColumns: '1fr 1fr 1fr',
        gap: '0.75rem',
        marginBottom: '1rem',
      }}>
        {/* Kill Switch */}
        <div style={{
          background: 'var(--bg-panel)',
          borderRadius: '10px',
          padding: '0.85rem',
          textAlign: 'center',
          border: isPaused ? '1px solid var(--accent-red)' : '1px solid var(--border)',
        }}>
          <div style={{
            fontFamily: 'var(--font-display)',
            fontSize: '0.8rem',
            fontWeight: 600,
            color: 'var(--text-muted)',
            marginBottom: '0.45rem',
          }}>
            🛑 Kill Switch
          </div>
          <button
            type="button"
            onClick={() => updateControl({ bot_status: isPaused ? 'RUNNING' : 'PAUSED' })}
            disabled={loading}
            style={{
              padding: '0.45rem 1.2rem',
              borderRadius: '8px',
              border: 'none',
              cursor: loading ? 'not-allowed' : 'pointer',
              fontFamily: 'var(--font-display)',
              fontWeight: 700,
              fontSize: '0.85rem',
              background: isPaused ? 'var(--accent-green)' : 'var(--accent-red)',
              color: isPaused ? '#000' : '#fff',
              opacity: loading ? 0.6 : 1,
              transition: 'all 0.2s',
            }}
          >
            {isPaused ? '▶ REANUDAR' : '⏸ PAUSAR'}
          </button>
          <div style={{
            fontFamily: 'var(--font-mono)',
            fontSize: '0.65rem',
            color: isPaused ? 'var(--accent-red)' : 'var(--accent-green)',
            marginTop: '0.35rem',
          }}>
            {isPaused ? 'BOT DETENIDO' : 'BOT ACTIVO'}
          </div>
        </div>

        {/* Min EV Threshold */}
        <div style={{
          background: 'var(--bg-panel)',
          borderRadius: '10px',
          padding: '0.85rem',
          textAlign: 'center',
          border: '1px solid var(--border)',
        }}>
          <div style={{
            fontFamily: 'var(--font-display)',
            fontSize: '0.8rem',
            fontWeight: 600,
            color: 'var(--text-muted)',
            marginBottom: '0.45rem',
          }}>
            📊 Min EV Threshold
          </div>
          <div style={{
            display: 'flex',
            alignItems: 'center',
            gap: '0.4rem',
            justifyContent: 'center',
          }}>
            <input
              type="range"
              min="0.02"
              max="0.30"
              step="0.01"
              value={control.min_ev_threshold}
              onChange={(e) => setControl({ ...control, min_ev_threshold: parseFloat(e.target.value) })}
              onMouseUp={() => updateControl({ min_ev_threshold: control.min_ev_threshold })}
              style={{ width: '100%', accentColor: 'var(--gold)' }}
            />
          </div>
          <input
            type="number"
            min="0.02"
            max="0.30"
            step="0.01"
            value={control.min_ev_threshold}
            onChange={(e) => {
              const v = parseFloat(e.target.value)
              setControl({ ...control, min_ev_threshold: isNaN(v) ? 0.08 : v })
            }}
            onBlur={() => updateControl({ min_ev_threshold: control.min_ev_threshold })}
            style={{
              width: '64px',
              textAlign: 'center',
              background: 'transparent',
              border: '1px solid var(--border)',
              color: 'var(--gold)',
              fontFamily: 'var(--font-mono)',
              fontSize: '1rem',
              fontWeight: 700,
              borderRadius: '6px',
              padding: '0.2rem',
              marginTop: '0.3rem',
            }}
          />
        </div>

        {/* Max Bets Per Match */}
        <div style={{
          background: 'var(--bg-panel)',
          borderRadius: '10px',
          padding: '0.85rem',
          textAlign: 'center',
          border: '1px solid var(--border)',
        }}>
          <div style={{
            fontFamily: 'var(--font-display)',
            fontSize: '0.8rem',
            fontWeight: 600,
            color: 'var(--text-muted)',
            marginBottom: '0.45rem',
          }}>
            🔢 Max Bets / Match
          </div>
          <div style={{ display: 'flex', gap: '0.3rem', justifyContent: 'center', alignItems: 'center' }}>
            <button
              type="button"
              onClick={() => {
                const v = Math.max(1, control.max_open_bets_per_match - 1)
                setControl({ ...control, max_open_bets_per_match: v })
                updateControl({ max_open_bets_per_match: v })
              }}
              style={{
                width: '28px', height: '28px',
                border: '1px solid var(--border)',
                background: 'var(--bg-card)',
                color: 'var(--text-main)',
                borderRadius: '6px',
                cursor: 'pointer',
                fontFamily: 'var(--font-mono)',
                fontSize: '1rem',
              }}
            >−</button>
            <span style={{
              fontFamily: 'var(--font-mono)',
              fontSize: '1.3rem',
              fontWeight: 700,
              color: 'var(--gold)',
              minWidth: '32px',
            }}>{control.max_open_bets_per_match}</span>
            <button
              type="button"
              onClick={() => {
                const v = Math.min(10, control.max_open_bets_per_match + 1)
                setControl({ ...control, max_open_bets_per_match: v })
                updateControl({ max_open_bets_per_match: v })
              }}
              style={{
                width: '28px', height: '28px',
                border: '1px solid var(--border)',
                background: 'var(--bg-card)',
                color: 'var(--text-main)',
                borderRadius: '6px',
                cursor: 'pointer',
                fontFamily: 'var(--font-mono)',
                fontSize: '1rem',
              }}
            >+</button>
          </div>
        </div>
      </div>

      {/* ── Trade Blotter Table ── */}
      <div>
        <div style={{
          display: 'flex',
          justifyContent: 'space-between',
          alignItems: 'center',
          marginBottom: '0.5rem',
        }}>
          <span style={{
            fontFamily: 'var(--font-display)',
            fontSize: '0.85rem',
            fontWeight: 600,
            color: 'var(--text-muted)',
          }}>
            📋 Trade Blotter
          </span>
          <span style={{
            fontFamily: 'var(--font-mono)',
            fontSize: '0.7rem',
            color: 'var(--text-muted)',
          }}>
            {executedBets.length} ejecutadas · {blockedBets.length} bloqueadas
          </span>
        </div>

        <div style={{
          overflow: 'auto',
          maxHeight: '200px',
          border: '1px solid var(--border)',
          borderRadius: '8px',
        }}>
          {blotter.length === 0 ? (
            <div style={{
              padding: '1.5rem',
              textAlign: 'center',
              color: 'var(--text-dim)',
              fontFamily: 'var(--font-mono)',
              fontSize: '0.8rem',
            }}>
              Sin operaciones registradas
            </div>
          ) : (
            <table style={{
              width: '100%',
              borderCollapse: 'collapse',
              fontFamily: 'var(--font-mono)',
              fontSize: '0.72rem',
            }}>
              <thead>
                <tr style={{
                  background: 'var(--bg-panel)',
                  color: 'var(--text-muted)',
                  position: 'sticky',
                  top: 0,
                }}>
                  <th style={thStyle}>Hora</th>
                  <th style={thStyle}>Partido</th>
                  <th style={thStyle}>Mercado</th>
                  <th style={thStyle}>Odds</th>
                  <th style={thStyle}>Model Prob</th>
                  <th style={thStyle}>EV Neto</th>
                  <th style={thStyle}>Stake</th>
                  <th style={thStyle}>Estado</th>
                </tr>
              </thead>
              <tbody>
                {blotter.map((row, i) => (
                  <tr key={i} style={{
                    borderBottom: '1px solid var(--border)',
                    background: i % 2 === 0 ? 'transparent' : 'var(--bg-panel)',
                  }}>
                    <td style={tdStyle}>{row.timestamp?.split(' ')[1] || row.timestamp || ''}</td>
                    <td style={{ ...tdStyle, maxWidth: '160px', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                      {row.match_url?.split('/').slice(-2).join('/') || ''}
                    </td>
                    <td style={tdStyle}>{row.market_and_line || ''}</td>
                    <td style={{ ...tdStyle, color: 'var(--accent-cyan)' }}>{row.odds_taken || ''}</td>
                    <td style={tdStyle}>{row.model_prob || ''}</td>
                    <td style={{ ...tdStyle, color: row.ev_neto && parseFloat(row.ev_neto) > 0 ? 'var(--accent-green)' : 'var(--accent-red)' }}>
                      {row.ev_neto || ''}
                    </td>
                    <td style={{ ...tdStyle, color: 'var(--gold)' }}>${row.stake || ''}</td>
                    <td style={{
                      ...tdStyle,
                      color: row.status === 'EXECUTED' ? 'var(--accent-green)' : 'var(--accent-red)',
                      fontWeight: 700,
                    }}>
                      {row.status || ''}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      </div>
    </div>
  )
}

const thStyle = {
  padding: '0.45rem 0.55rem',
  textAlign: 'left',
  borderBottom: '1px solid var(--border-strong)',
  fontWeight: 700,
  fontSize: '0.68rem',
}

const tdStyle = {
  padding: '0.38rem 0.55rem',
  color: 'var(--text-main)',
}
