import { useState } from 'react'
import { useBetLock } from './useBetLock'
import { simulateMatch, overrideMatch } from '../services/api'
import DetailGrid from './DetailGrid'
import AdjustPanel from './AdjustPanel'

function fmtAge(seconds) {
  if (seconds < 60) return `${seconds}s`
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ${seconds % 60}s`
  return `${Math.floor(seconds / 3600)}h ${Math.floor((seconds % 3600) / 60)}m`
}

export default function MarketSignal({ title, decisionData, matchUrl, settings, currentCount, faroStale }) {
  const [detailOpen, setDetailOpen] = useState(false)
  const [editing, setEditing] = useState(false)
  const [customLine, setCustomLine] = useState(decisionData?.linea || 0)
  const [customOver, setCustomOver] = useState(decisionData?.raw_over || '')
  const [customUnder, setCustomUnder] = useState(decisionData?.raw_under || '')
  const [simming, setSimming] = useState(false)
  const [simResult, setSimResult] = useState(null)
  const [showReason, setShowReason] = useState(false)
  const { lock, isLocked, placeLock, releaseLock, placing } = useBetLock(decisionData, matchUrl, title)

  if (!decisionData) return null

  const isNoBet = decisionData.best_side === 'NO BET' || decisionData.best_side === 'PASAR'
  const stakePct = ((decisionData.best_stake || 0) * 100)

  let strength = 'nobet'
  if (!isNoBet) {
    if (decisionData.best_stake >= 0.035) strength = 'strong'
    else if (decisionData.best_stake >= 0.02) strength = 'medium'
    else strength = 'soft'
  }

  const mktLabel = { Goles: '⚽', Corners: '🚩', Tarjetas: '🟨' }[title] || '◉'

  const handleSave = async (sim = false) => {
    if (sim) setSimming(true)
    try {
      const fn = sim ? simulateMatch : overrideMatch
      const d = await fn(matchUrl, { market: title.toUpperCase(), linea: parseFloat(customLine), over: parseFloat(customOver), under: parseFloat(customUnder) })
      if (d.status === 'ok' && d.data) {
        if (sim) setSimResult(d.data.result?.markets?.[title.toLowerCase()]?.decision)
        else setEditing(false)
      }
    } catch { /* noop */ } finally { setSimming(false) }
  }

  const fairOdds = decisionData.fair_over
  const bookOdds = decisionData.raw_over
  const edge = (fairOdds && bookOdds) ? ((fairOdds / bookOdds - 1) * 100) : null

  if (isNoBet) {
    return (
      <div className={`signal-row nobet ${faroStale ? 'faro-stale' : ''}`}>
        <span className="signal-mkt-icon">{mktLabel}</span>
        <span className="signal-mkt-name">{title}</span>
        <span className="signal-nobet-label">PASAR</span>
        <button className="signal-detail-btn" onClick={() => setDetailOpen(o => !o)}>
          {detailOpen ? '▲' : '▼'}
        </button>
        {detailOpen && (
          <div className="signal-detail-panel">
            <DetailGrid decisionData={decisionData} currentCount={currentCount} edge={edge} />
            <AdjustPanel editing={editing} setEditing={setEditing} customLine={customLine} setCustomLine={setCustomLine}
              customOver={customOver} setCustomOver={setCustomOver} customUnder={customUnder} setCustomUnder={setCustomUnder}
              simming={simming} handleSave={handleSave} simResult={simResult} decisionData={decisionData}
              showReason={showReason} setShowReason={setShowReason} />
          </div>
        )}
      </div>
    )
  }

  const stakeUsd = (decisionData.best_stake || 0) * settings.bankroll
  const handlePlaceLock = () => placeLock(
    decisionData.best_side, decisionData.linea,
    decisionData.best_side === 'OVER' ? decisionData.raw_over : decisionData.raw_under,
    stakeUsd
  )

  return (
    <div className={`signal-row active ${strength} ${faroStale ? 'faro-stale' : ''} ${isLocked ? 'bet-locked' : ''}`}>
      <div className="signal-active-main">
        <div className="signal-active-left">
          <span className="signal-mkt-icon-big">{mktLabel}</span>
          <div className="signal-active-labels">
            <div className="signal-mkt-name-big">{title}</div>
            <div className="signal-act-current">Actual: {currentCount}</div>
          </div>
        </div>

        <div className="signal-action-center">
          <div className={`signal-action-chip ${strength}`}>
            {decisionData.best_side}
          </div>
          <div className="signal-linea-val">{decisionData.linea}</div>
        </div>

        <div className="signal-stake-block">
          <div className="signal-stake-usd">${stakeUsd.toFixed(0)}</div>
          <div className="signal-stake-pct">{stakePct.toFixed(1)}% Kelly</div>
        </div>

        {!isLocked ? (
          <button
            className="bet-lock-btn place"
            onClick={handlePlaceLock}
            disabled={placing || faroStale}
            title={faroStale ? 'Safe Mode activo — no se puede apostar sin el Faro' : 'Registrar apuesta y bloquear señal'}
          >
            {placing ? '…' : '🔒 Aposté'}
          </button>
        ) : (
          <button
            className="bet-lock-btn release"
            onClick={releaseLock}
            title="Marcar apuesta como resuelta"
          >
            ✓ Resolver
          </button>
        )}

        <button className="signal-detail-btn" onClick={() => setDetailOpen(o => !o)}>
          {detailOpen ? '▲' : '▼'}
        </button>
      </div>

      {isLocked && (
        <div className="bet-lock-banner">
          <span className="bet-lock-icon">🔒</span>
          <span className="bet-lock-info">
            Apostado hace <strong>{fmtAge(lock.locked_ago_s || 0)}</strong>
            {lock.stake_usd > 0 && <> — <strong>${lock.stake_usd.toFixed(0)}</strong></>}
            {lock.odds > 0 && <> @ <strong>{lock.odds.toFixed(2)}</strong></>}
          </span>
          <span className="bet-lock-exp">Expira en {fmtAge(lock.expires_in_s || 0)}</span>
        </div>
      )}

      {detailOpen && (
        <div className="signal-detail-panel">
          <DetailGrid decisionData={decisionData} currentCount={currentCount} edge={edge} bookOdds={bookOdds} />
          <AdjustPanel editing={editing} setEditing={setEditing} customLine={customLine} setCustomLine={setCustomLine}
            customOver={customOver} setCustomOver={setCustomOver} customUnder={customUnder} setCustomUnder={setCustomUnder}
            simming={simming} handleSave={handleSave} simResult={simResult} decisionData={decisionData}
            showReason={showReason} setShowReason={setShowReason} />
        </div>
      )}
    </div>
  )
}
