import { useState, useCallback } from 'react'

/**
 * KellyCalculator – calculadora Kelly interactiva estilo dashboard de referencia.
 * Conectada al bankroll real de settings.
 */
export default function KellyCalculator({ settings, onSave }) {
  const bankroll = settings?.bankroll || 1000
  const defaultFrac = Math.round((settings?.kelly_fraction || 0.1) * 100)

  const [odds, setOdds]     = useState(1.9)
  const [prob, setProb]     = useState(55)
  const [frac, setFrac]     = useState(defaultFrac)

  const compute = useCallback(() => {
    const b = odds - 1
    const p = prob / 100
    const q = 1 - p
    const kellyFull = (b * p - q) / b
    const kellyFrac = Math.max(0, kellyFull) * (frac / 100)
    const stake = kellyFrac * bankroll
    const evPct  = (p * odds - 1) * 100
    return { stake, kellyFrac, evPct }
  }, [odds, prob, frac, bankroll])

  const { stake, kellyFrac, evPct } = compute()
  const isPositiveEv = evPct > 0

  return (
    <div className="dash-panel kelly-panel">
      <div className="dash-panel-header">
        <span className="dash-panel-title">Calculadora Kelly</span>
        <span className="tag tag-gold">Gestión Bankroll</span>
      </div>
      <div className="kelly-body">
        <div className="kelly-row">
          <div className="kelly-field-wrap">
            <label className="kelly-label">Cuota decimal</label>
            <input
              className="kelly-field"
              type="number"
              value={odds}
              step="0.05"
              min="1.01"
              onChange={e => setOdds(parseFloat(e.target.value) || 1.5)}
            />
          </div>
          <div className="kelly-field-wrap">
            <label className="kelly-label">Win Prob. (%)</label>
            <input
              className="kelly-field"
              type="number"
              value={prob}
              step="1"
              min="1"
              max="99"
              onChange={e => setProb(parseFloat(e.target.value) || 50)}
            />
          </div>
        </div>

        <div className="kelly-field-wrap" style={{ marginTop: '10px' }}>
          <label className="kelly-label">
            Fracción Kelly — <span style={{ color: 'var(--gold, var(--accent-yellow))' }}>{frac}%</span>
          </label>
          <input
            type="range" min="10" max="100" step="5" value={frac}
            onChange={e => setFrac(parseInt(e.target.value))}
            style={{
              width: '100%', accentColor: 'var(--gold, var(--accent-yellow))',
              cursor: 'pointer', height: '4px',
            }}
          />
          <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: '9px', color: 'var(--text-dim)', marginTop: '2px' }}>
            <span>Conservador (10%)</span><span>Completo (100%)</span>
          </div>
        </div>

        {/* EV indicator */}
        <div style={{
          display: 'flex', alignItems: 'center', gap: '6px', marginTop: '10px',
          padding: '6px 10px', borderRadius: '6px',
          background: isPositiveEv ? 'rgba(46,204,113,0.08)' : 'rgba(231,76,60,0.08)',
          border: `1px solid ${isPositiveEv ? 'rgba(46,204,113,0.25)' : 'rgba(231,76,60,0.25)'}`,
        }}>
          <span style={{ fontSize: '11px', color: isPositiveEv ? 'var(--accent-green)' : 'var(--accent-red)', fontWeight: 700 }}>
            EV: {evPct >= 0 ? '+' : ''}{evPct.toFixed(1)}%
          </span>
          <span style={{ fontSize: '10px', color: 'var(--text-dim)', flex: 1, textAlign: 'right' }}>
            {isPositiveEv ? '✓ Valor positivo' : '✗ Sin valor'}
          </span>
        </div>

        {/* Result box */}
        <div className="kelly-result">
          <div>
            <div className="kelly-result-label">Stake recomendado</div>
            <div style={{ fontSize: '10px', color: 'var(--text-dim)', marginTop: '2px' }}>
              {(kellyFrac * 100).toFixed(1)}% del bankroll
            </div>
          </div>
          <div style={{ textAlign: 'right' }}>
            <div className="kelly-result-val">${stake.toFixed(2)}</div>
            <div style={{ fontSize: '10px', color: 'var(--text-muted)', marginTop: '2px' }}>
              Bankroll: ${bankroll.toLocaleString('es-CO')}
            </div>
          </div>
        </div>
      </div>
    </div>
  )
}
