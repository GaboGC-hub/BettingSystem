import React, { useState, useCallback, useEffect } from 'react'
import AttackHeatmap from './AttackHeatmap'

const API = 'http://localhost:8000/api'

// ─── Bet Lock Hook ────────────────────────────────────────────────────
function useBetLock(decisionData, matchUrl, market) {
  const serverLock = decisionData?.bet_lock || null
  const [localLock, setLocalLock] = useState(serverLock)
  const [placing, setPlacing] = useState(false)

  // Sync con datos del servidor en cada render (el poll del backend actualiza el lock)
  useEffect(() => { setLocalLock(serverLock) }, [JSON.stringify(serverLock)])

  const lock = localLock
  const isLocked = Boolean(lock)

  const placeLock = useCallback(async (side, linea, odds, stakeUsd) => {
    setPlacing(true)
    try {
      const res = await fetch(`${API}/bets/lock`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ match_url: matchUrl, market, linea, side, odds, stake_usd: stakeUsd, source: 'manual' })
      })
      const d = await res.json()
      if (d.status === 'ok') {
        setLocalLock({ lock_id: d.lock_id, locked_at: Date.now() / 1000, locked_ago_s: 0, expires_in_s: 5400, stake_usd: stakeUsd, odds })
      }
    } catch (e) { console.error('[BET LOCK] place failed', e) }
    finally { setPlacing(false) }
  }, [matchUrl, market])

  const releaseLock = useCallback(async () => {
    if (!lock) return
    try {
      await fetch(`${API}/bets/lock/${lock.lock_id}`, { method: 'DELETE' })
      setLocalLock(null)
    } catch (e) { console.error('[BET LOCK] release failed', e) }
  }, [lock])

  return { lock, isLocked, placeLock, releaseLock, placing }
}

// Formatea segundos como "2m 15s" o "1h 3m"
function fmtAge(seconds) {
  if (seconds < 60) return `${seconds}s`
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ${seconds % 60}s`
  return `${Math.floor(seconds / 3600)}h ${Math.floor((seconds % 3600) / 60)}m`
}

// ─── Market Signal Block ───────────────────────────────────────────────────
function MarketSignal({ title, decisionData, matchUrl, settings, currentCount, faroStale }) {
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
  const stakeUSD = ((decisionData.best_stake || 0) * settings.bankroll)
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
      const res = await fetch(`${API}/matches/${sim ? 'simulate' : 'override'}`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ url: matchUrl, market: title.toUpperCase(), linea: parseFloat(customLine), over: parseFloat(customOver), under: parseFloat(customUnder) })
      })
      const d = await res.json()
      if (d.status === 'ok' && d.data) {
        if (sim) setSimResult(d.data.result?.markets?.[title.toLowerCase()]?.decision)
        else setEditing(false)
      }
    } catch(e) {} finally { setSimming(false) }
  }

  const fairOdds = decisionData.fair_over
  const bookOdds = decisionData.raw_over
  const edge = (fairOdds && bookOdds) ? ((fairOdds / bookOdds - 1) * 100) : null

  // ── PASAR: dim row ──
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

  // ── SEÑAL ACTIVA: Big action block ──
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

        {/* Lock/Unlock buttons */}
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

      {/* Banner de lock activo */}
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

function DetailGrid({ decisionData, currentCount, edge, bookOdds }) {
  const edgeColor = edge > 5 ? 'var(--accent-green)' : edge > 2 ? 'var(--accent-yellow)' : 'var(--text-dim)'
  const hasBookie = decisionData.raw_over || decisionData.raw_under
  return (
    <div className="detail-grid-inner">
      <div className="detail-row"><span className="dlbl">Actual</span><span className="dval">{currentCount}</span></div>
      <div className="detail-row"><span className="dlbl">Linea</span><span className="dval">{decisionData.linea}</span></div>
      <div className="detail-row"><span className="dlbl">Prob Over</span><span className="dval">{(decisionData.prob_over*100).toFixed(1)}%</span></div>
      <div className="detail-row"><span className="dlbl">Prob Under</span><span className="dval">{(decisionData.prob_under*100).toFixed(1)}%</span></div>
      <div className="detail-row"><span className="dlbl">Fair Over</span><span className="dval">{decisionData.fair_over?.toFixed(3)}</span></div>
      <div className="detail-row"><span className="dlbl">Fair Under</span><span className="dval">{decisionData.fair_under?.toFixed(3)}</span></div>
      {hasBookie && (
        <>
          <div className="detail-row" style={{borderTop:'1px solid var(--hard-border)',marginTop:'0.2rem',paddingTop:'0.3rem',gridColumn:'1 / -1'}}>
            <span className="dlbl" style={{color:'var(--accent-yellow)',fontWeight:700}}>
              {(() => {
                const sid = decisionData.source_id || ''
                if (sid === 'pinnacle' || sid === 'EXT_WS_PS3838') return '🟠 CUOTA BETANO'
                if (sid === 'betano') return '⚪ ESTIMADO (Soft)'
                if (sid === 'betano_dejuiced') return '🟠 BETANO (de-juiced)'
                if (sid === 'betano_snapshot') return '⚪ ESTIMADO (Base)'
                if (sid === 'betplay') return '🔵 CUOTA BETPLAY'
                return '⚪ ESTIMADO (sin cuota real)'
              })()}
            </span>
            <span className="dval" style={{color:'var(--text-dim)',fontSize:'0.6rem'}}>
              {decisionData.source_id || 'sin fuente'}
            </span>
          </div>
          <div className="detail-row">
            <span className="dlbl">Over</span>
            <span className="dval" style={{color:'var(--text-main)',fontWeight:600}}>
              {decisionData.raw_over?.toFixed(3) ?? '—'}
            </span>
          </div>
          <div className="detail-row">
            <span className="dlbl">Under</span>
            <span className="dval" style={{color:'var(--text-main)',fontWeight:600}}>
              {decisionData.raw_under?.toFixed(3) ?? '—'}
            </span>
          </div>
        </>
      )}
      {edge !== null && (
        <div className="detail-row">
          <span className="dlbl">Edge vs Pinnacle</span>
          <span className="dval" style={{color: edgeColor}}>{edge > 0 ? '+':''}{edge?.toFixed(1)}%</span>
        </div>
      )}
    </div>
  )
}

function AdjustPanel({ editing, setEditing, customLine, setCustomLine, customOver, setCustomOver, customUnder, setCustomUnder, simming, handleSave, simResult, decisionData, showReason, setShowReason }) {
  return (
    <>
      {!editing ? (
        <button className="reason-toggle" style={{marginTop:'0.4rem'}} onClick={e => { e.stopPropagation(); setEditing(true) }}>✏ Ajustar cuotas manualmente</button>
      ) : (
        <div className="edit-inline" onClick={e => e.stopPropagation()}>
          <div className="ef"><label>Línea</label><input type="number" step="0.5" value={customLine} onChange={e=>setCustomLine(e.target.value)}/></div>
          <div className="ef"><label>Over</label><input type="number" step="0.01" value={customOver} onChange={e=>setCustomOver(e.target.value)}/></div>
          <div className="ef"><label>Under</label><input type="number" step="0.01" value={customUnder} onChange={e=>setCustomUnder(e.target.value)}/></div>
          <button className="xs" onClick={()=>handleSave(false)}>✔</button>
          <button className="xs secondary" onClick={()=>handleSave(true)} disabled={simming}>{simming?'…':'🧪'}</button>
          <button className="xs secondary" onClick={()=>setEditing(false)}>×</button>
        </div>
      )}
      {simResult && (
        <div className="sim-box" onClick={e=>e.stopPropagation()}>
          <div className="sim-tag">🧪 Calculadora Fantasma</div>
          {simResult.best_side} {simResult.linea} — Prob {(simResult.best_prob*100).toFixed(1)}% — EV {simResult.best_ev?.toFixed(3)}
        </div>
      )}
      {decisionData.reasoning && (
        <>
          <button className="reason-toggle" onClick={e=>{e.stopPropagation();setShowReason(r=>!r)}}>
            <span style={{fontSize:'0.55rem'}}>{showReason?'▼':'▶'}</span> ¿Por qué esta apuesta?
          </button>
          {showReason && (
            <div className="reason-body" onClick={e=>e.stopPropagation()}>
              {decisionData.reasoning.split(' | ').map((line,i)=>(
                <div key={i} style={{marginBottom:'0.2rem'}}>• {line}</div>
              ))}
            </div>
          )}
        </>
      )}
    </>
  )
}

// ─── Tension Bar ──────────────────────────────────────────────────────────
function TensionBar({ value }) {
  const pct = Math.min(100, (value / 0.2) * 100)
  const color = pct > 70 ? '#ef4444' : pct > 40 ? '#f59e0b' : 'var(--accent-cyan)'
  const label = pct > 70 ? 'ALTA' : pct > 40 ? 'MEDIA' : 'BAJA'
  return (
    <div className="zen-tension">
      <div className="zen-tension-track">
        <div className="zen-tension-fill" style={{ width: `${pct}%`, background: color }}/>
      </div>
      <span className="zen-tension-label" style={{ color }}>⚡ {label}</span>
    </div>
  )
}

// ─── DebugRow: fila del panel de sensores ─────────────────────────────────
function DebugRow({ label, ago, ok, na }) {
  const pending = !na && ago == null
  const color = na ? 'var(--text-dim)' : pending ? 'var(--text-muted)' : ok ? 'var(--accent-green)' : 'var(--accent-red)'
  const badge = na ? '—  Sin datos' : pending ? '—  Esperando tick' : ok ? `✅ hace ${ago}s` : `⛔ hace ${ago}s (STALE)`
  return (
    <div className="detail-row">
      <span className="dlbl">{label}</span>
      <span className="dval" style={{ color, fontFamily: 'monospace' }}>{badge}</span>
    </div>
  )
}

// ─── MatchCard ─────────────────────────────────────────────────────────────
export default function MatchCard({ url, data, settings, onRemove, onScraperAttached, onScraperUpdate, scraperMeta }) {
  const isReady = data?.snapshot && data?.state && data?.result
  const snap    = isReady ? data.snapshot : {}
  const state   = isReady ? data.state : {}
  const result  = isReady ? data.result : {}
  const phase   = isReady ? data.phase_summary : ''
  const dq      = data?.data_quality || {}
  const sofa_ts = data?.sofascore_ts || 0
  const scr_ts  = data?.scraper_ts   || 0

  const nowSec = Date.now() / 1000
  const sofaLagS   = sofa_ts  ? Math.floor(nowSec - sofa_ts)  : null
  const scraperLagS = scr_ts  ? Math.floor(nowSec - scr_ts)   : null
  const faroPulse = dq.faro_pulse || {}
  const staleSec = dq.faro_stale_seconds ?? 45
  const anyFaroStale = dq.pinnacle_stale === true
  const goalFaroStale = !!faroPulse.GOLES?.stale
  const cornerFaroStale = !!faroPulse.CORNERS?.stale
  const cardFaroStale = !!faroPulse.TARJETAS?.stale
  // Safe Mode: solo bloquear si Pinnacle ESTUVO activo y ahora está stale.
  // Si nunca estuvo activo, el modelo corre sin Faro (no es un error, es modo extension-only).
  const isSafeMode = dq.pinnacle_active && anyFaroStale

  const [debugOpen, setDebugOpen] = useState(false)

  const [statsOpen, setStatsOpen] = useState(false)
  const [scraperOpen, setScraperOpen] = useState(false)
  const [isRefreshing, setIsRefreshing] = useState(false)
  const [editStats, setEditStats] = useState({
    xg_local:0, xg_visitante:0, corners:0, faltas:0,
    centros_local:0, centros_visitante:0, amarillas:0, rojas:0
  })

  React.useEffect(() => {
    if (isReady && !statsOpen) setEditStats({
      xg_local: state.xg_local||0, xg_visitante: state.xg_visitante||0,
      corners: state.corners||0, faltas: state.faltas||0,
      centros_local: state.centros_local||0, centros_visitante: state.centros_visitante||0,
      amarillas: state.amarillas||0, rojas: state.rojas||0
    })
  }, [state, statsOpen, isReady])

  const handleSaveStats = async () => {
    try {
      await fetch(`${API}/matches/override_stats`, { method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({ url, stats: editStats }) })
      setStatsOpen(false)
    } catch(e) {}
  }

  const isEnded = snap.status_text?.toLowerCase()?.includes('ended') || snap.status_text?.toLowerCase()?.includes('ft')
  const tension = result.tension_index || 0

  const goalDec   = result.markets?.goles?.decision
  const cornerDec = result.markets?.corners?.decision
  const cardDec   = result.markets?.tarjetas?.decision

  const hasSignal = [goalDec, cornerDec, cardDec].some(d => d && d.best_side !== 'NO BET' && d.best_side !== 'PASAR')
  const scraperActive = scraperMeta?.active
  const scraperAgo = scraperMeta?.last_seen ? Math.floor(Date.now()/1000 - scraperMeta.last_seen) : null

  // ── Smart money whale indicator ──
  const hasWhale = [goalDec, cornerDec, cardDec].some(d => d?.note?.includes('LAG SNIPER'))

  if (!isReady) {
    const rawHost = url.replace(/^https?:\/\//, '')
    const shortUrl = rawHost.length > 56 ? `${rawHost.slice(0, 54)}…` : rawHost
    return (
      <div className="connecting-card connecting-card--rich" role="status" aria-live="polite" aria-busy="true">
        <div className="connecting-card__preview">
          <div className="connecting-shimmer connecting-shimmer--score" />
          <div className="connecting-shimmer connecting-shimmer--teams" />
          <div className="connecting-shimmer connecting-shimmer--meta" />
        </div>
        <div className="connecting-spinner connecting-spinner--lg" aria-hidden />
        <div className="connecting-card__title">Sincronizando partido</div>
        <div className="connecting-card__sub">Leyendo estado en vivo y calidad de datos desde SofaScore…</div>
        <div className="connecting-card__url mono" title={url}>{shortUrl}</div>
        <button type="button" className="secondary xs connecting-card__cancel" onClick={() => onRemove(url)}>Cancelar</button>
      </div>
    )
  }

  return (
    <div className={`match-card ${hasSignal ? 'has-signal' : ''} ${isEnded ? 'ended' : ''} ${anyFaroStale ? 'match-card--faro-stale' : ''} ${isRefreshing ? 'match-card--refreshing' : ''}`}>

      {/* ── TENSION STRIP TOP ── */}
      <TensionBar value={tension} />

      {/* ── HEADER ── */}
      <div className="card-header">
        <div className="card-meta">
          <div className={`dot ${isEnded ? 'ended' : 'live'}`}/>
          <span>{snap.tournament}</span>
          <span className="card-meta-right">
            <span className="live-minute">{snap.minute}'</span>
          </span>
        </div>

        <div className="score-block-zen">
          <div className="team-block">
            <span className="team-name">{snap.home_team}</span>
            <span className="team-goal">{snap.goals_home}</span>
          </div>
          <div className="score-divider">vs</div>
          <div className="team-block away">
            <span className="team-goal away">{snap.goals_away}</span>
            <span className="team-name away">{snap.away_team}</span>
          </div>
        </div>

        {/* Stats pill row */}
        <div className="stats-pill-row">
          <StatPill label="xG" value={`${(state.xg_local||0).toFixed(1)}–${(state.xg_visitante||0).toFixed(1)}`} />
          <StatPill label="CRN" value={state.corners} />
          <StatPill label="TAR" value={(state.amarillas||0) + (state.rojas||0)} accent={(state.amarillas||0)+(state.rojas||0) >= 4 ? 'yellow' : null}/>
          <StatPill label="FOUL" value={state.faltas} />
          {state.rojas > 0 && <StatPill label="ROJA" value={state.rojas} accent="red"/>}
          {hasWhale && <span className="whale-badge">🐋 Smart $</span>}
        </div>
      </div>

      {/* ── ATTACK HEATMAP (colapsado) ── */}
      <AttackHeatmap state={state} result={result} snap={snap} lagActive={cardDec?.note?.includes('LAG SNIPER')} dataQuality={dq}/>

      {/* ── SAFE MODE BANNER ── */}
      {isSafeMode && (
        <div style={{
          display: 'flex', alignItems: 'center', gap: '0.5rem',
          padding: '0.5rem 1rem',
          background: 'rgba(246,70,93,0.08)',
          borderTop: '1px solid rgba(246,70,93,0.3)',
          borderBottom: '1px solid rgba(246,70,93,0.3)',
        }}>
          <span style={{ fontSize: '0.9rem' }}>⏱️</span>
          <div style={{ flex: 1 }}>
            <div style={{ fontSize: '0.68rem', fontWeight: 700, color: 'var(--accent-red)', letterSpacing: '0.06em' }}>
              SAFE MODE — SEÑALES SUSPENDIDAS
            </div>
            <div style={{ fontSize: '0.6rem', color: 'var(--text-muted)', marginTop: '0.1rem' }}>
              Uno o más mercados del Faro superan {staleSec}s sin actualizar (revisá Sensores). Kelly en 0 para esos mercados.
            </div>
          </div>
          <div style={{
            fontFamily: 'monospace', fontSize: '1.1rem', fontWeight: 800,
            color: 'var(--accent-red)', animation: 'kpi-pulse 1s ease-in-out infinite'
          }}>
            {scraperLagS}s
          </div>
        </div>
      )}

      {/* ── MERCADOS: SEÑALES ── */}
      <div className="signal-list">
        <MarketSignal title="Goles"    decisionData={goalDec}   matchUrl={url} settings={settings} currentCount={(state.goles_local||0)+(state.goles_visitante||0)} faroStale={goalFaroStale}/>
        <MarketSignal title="Corners"  decisionData={cornerDec} matchUrl={url} settings={settings} currentCount={state.corners||0} faroStale={cornerFaroStale}/>
        <MarketSignal title="Tarjetas" decisionData={cardDec}   matchUrl={url} settings={settings} currentCount={(state.amarillas||0)+((state.rojas||0)*2)} faroStale={cardFaroStale}/>
      </div>

      {/* ── FOOTER ── */}
      <div className="card-footer">
        <div className="card-footer-phase" title={phase}>{phase || 'Analizando…'}</div>
        <div className="card-actions">
          {!isEnded && <button className="secondary xs" onClick={()=>setStatsOpen(true)} title="Editar stats">✏</button>}
          {!isEnded && <button className="secondary xs" onClick={()=>setScraperOpen(true)} title="Estado extensión">📡</button>}
          {!isEnded && (
            <button
              type="button"
              className={`secondary xs match-card-refresh-btn ${isRefreshing ? 'is-busy' : ''}`}
              disabled={isRefreshing}
              onClick={async()=>{
                setIsRefreshing(true)
                try {
                  await fetch(`${API}/matches/refresh`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({url})})
                  onScraperUpdate?.(url)
                } catch (e) {}
                finally { setTimeout(()=>setIsRefreshing(false), 1200) }
              }}
              title="Forzar recálculo (SofaScore)"
            >
              {isRefreshing ? <span className="btn-inline-spinner" aria-hidden /> : <span className="match-card-refresh-ico" aria-hidden>🔄</span>}
              <span className="sr-only">{isRefreshing ? 'Actualizando…' : 'Actualizar'}</span>
            </button>
          )}
          <button className="secondary xs danger" onClick={()=>onRemove(url)} title="Cerrar">✕</button>
        </div>
      </div>

      {/* ── SENSOR DEBUG PANEL ── */}
      <div
        style={{
          borderTop: '1px solid var(--hard-border)',
          background: 'var(--bg-void)',
        }}
      >
        <button
          className="reason-toggle"
          style={{ width:'100%', padding:'0.35rem 1rem', justifyContent:'flex-start', gap:'0.4rem',
                   color: debugOpen ? 'var(--text-muted)' : 'var(--text-dim)' }}
          onClick={() => setDebugOpen(o => !o)}
        >
          <span style={{ fontSize: '0.55rem' }}>{debugOpen ? '▼' : '▶'}</span>
          📡 Sensores
          {isSafeMode && <span style={{ color:'var(--accent-red)', fontWeight:700, marginLeft:'auto' }}>⛔ STALE</span>}
          {!isSafeMode && dq.pinnacle_active && <span style={{ color:'var(--accent-green)', marginLeft:'auto' }}>✅ LIVE</span>}
          {!dq.pinnacle_active && <span style={{ color:'var(--text-dim)', marginLeft:'auto' }}>— Sin Faro</span>}
        </button>

        {debugOpen && (
          <div style={{ padding:'0.5rem 1rem 0.6rem', display:'flex', flexDirection:'column', gap:'0.3rem' }}>
            <div className="detail-row" style={{ opacity: 0.85, fontSize: '0.58rem', color: 'var(--text-dim)' }}>
              <span>Pulso Faro</span>
              <span style={{ fontFamily: 'monospace' }}>umbral {staleSec}s</span>
            </div>
            <DebugRow label="SofaScore" ago={sofaLagS} ok={sofaLagS !== null && sofaLagS < 30} />
            <DebugRow
              label="Pinnacle (Faro)"
              ago={dq.pinnacle_active ? scraperLagS : null}
              ok={dq.pinnacle_active && !anyFaroStale}
              na={!dq.pinnacle_active}
            />
            {dq.pinnacle_active && (
              <>
                <DebugRow label="Faro — Goles" ago={faroPulse.GOLES?.lag_s ?? null} ok={!goalFaroStale} na={false} />
                <DebugRow label="Faro — Corners" ago={faroPulse.CORNERS?.lag_s ?? null} ok={!cornerFaroStale} na={false} />
                <DebugRow label="Faro — Tarjetas" ago={faroPulse.TARJETAS?.lag_s ?? null} ok={!cardFaroStale} na={false} />
              </>
            )}
            <div className="detail-row">
              <span className="dlbl">Toques en área</span>
              <span className="dval" style={{ color: dq.touches_real ? 'var(--text-main)' : 'var(--accent-yellow)' }}>
                {dq.touches_home ?? 0} + {dq.touches_away ?? 0}
                {!dq.touches_real && ' ⚠ EST.'}
              </span>
            </div>
            {dq.pinnacle_active && (
              <>
                <div className="detail-row">
                  <span className="dlbl">Fair Over — Goles</span>
                  <span className="dval">{dq.raw_over_goles?.toFixed(3) ?? '—'}</span>
                </div>
                <div className="detail-row">
                  <span className="dlbl">Fair Over — Corners</span>
                  <span className="dval">{dq.raw_over_corners?.toFixed(3) ?? '—'}</span>
                </div>
                <div className="detail-row">
                  <span className="dlbl">Fair Over — Tarjetas</span>
                  <span className="dval">{dq.raw_over_tarjetas?.toFixed(3) ?? '—'}</span>
                </div>
              </>
            )}
          </div>
        )}
      </div>

      {/* ── SCRAPER STATUS (legacy badge) ── */}
      <div className={`scraper-badge ${scraperActive ? 'live' : ''}`}>
        <div className={`dot ${scraperActive ? 'active' : 'idle'}`}/>
        {scraperActive ? `Cuotas live (+${scraperAgo}s)` : 'Scraper inactivo — clic 🕷 para inyectar URLs'}
      </div>

      {/* ── MODALS ── */}
      {statsOpen && (
        <div className="modal-backdrop" onClick={e=>e.target===e.currentTarget&&setStatsOpen(false)}>
          <div className="modal-box">
            <div className="modal-title">✏ Estadísticas</div>
            <div className="modal-sub">Corrige los datos del partido manualmente.</div>
            <div className="form-grid">
              {[['xg_local','xG Local','0.01'],['xg_visitante','xG Visitante','0.01'],['corners','Corners','1'],['faltas','Faltas','1'],['centros_local','Centros L','1'],['centros_visitante','Centros V','1'],['amarillas','Amarillas','1'],['rojas','Rojas','1']].map(([k,l,s])=>(
                <div className="form-field" key={k}>
                  <label>{l}</label>
                  <input type="number" step={s} value={editStats[k]} onChange={e=>setEditStats({...editStats,[k]:parseFloat(e.target.value)||0})}/>
                </div>
              ))}
            </div>
            <div style={{display:'flex',gap:'0.6rem',marginTop:'1.25rem'}}>
              <button onClick={handleSaveStats} style={{flex:1}}>Guardar</button>
              <button className="secondary" onClick={()=>setStatsOpen(false)} style={{flex:1}}>Cancelar</button>
            </div>
          </div>
        </div>
      )}

      {scraperOpen && (
        <div className="modal-backdrop" onClick={e=>e.target===e.currentTarget&&setScraperOpen(false)}>
          <div className="modal-box">
            <div className="modal-title">📡 Estado de la Extensión</div>

            <div style={{background:'var(--bg-void)',border:'1px solid var(--hard-border)',borderRadius:'6px',padding:'0.85rem',marginBottom:'1rem'}}>
              <div style={{fontSize:'0.7rem',color:'var(--accent-cyan)',fontWeight:700,marginBottom:'0.5rem',letterSpacing:'0.06em'}}>⚡ MODO EXTENSIÓN — Cero Riesgo de Ban</div>
              <div style={{fontSize:'0.72rem',color:'var(--text-dim)',lineHeight:1.6}}>
                El sistema recibe datos directamente desde tus pestañas del navegador.<br/>
                <strong style={{color:'var(--text-main)'}}>No necesitas pegar URLs.</strong> Solo abre:
                <ul style={{margin:'0.5rem 0 0 1rem',padding:0}}>
                  <li>📊 <strong>SofaScore</strong> — estadísticas en vivo (tiros, posesión, xG)</li>
                  <li>🟠 <strong>Betano</strong> — cuotas Over/Under en vivo</li>
                  <li>🔵 <strong>Betplay</strong> — cuotas Over/Under en vivo</li>
                  <li>⬛ <strong>PS3838</strong> — cuotas Pinnacle (Faro de referencia)</li>
                </ul>
              </div>
            </div>

            <div style={{background:'var(--bg-void)',border:'1px solid var(--hard-border)',borderRadius:'6px',padding:'0.75rem',marginBottom:'1rem'}}>
              <div style={{fontSize:'0.68rem',color:'var(--text-dim)',fontWeight:700,marginBottom:'0.4rem'}}>ESTADO ACTUAL</div>
              <div style={{display:'grid',gridTemplateColumns:'1fr 1fr',gap:'0.4rem',fontSize:'0.72rem'}}>
                <div style={{color: dq.scraper_lag_s != null && dq.scraper_lag_s < 20 ? 'var(--accent-green)' : 'var(--text-muted)'}}>
                  📊 SofaScore: {dq.scraper_lag_s != null ? `${dq.scraper_lag_s}s` : 'esperando…'}
                </div>
                <div style={{color: dq.pinnacle_active ? 'var(--accent-green)' : 'var(--text-muted)'}}>
                  ⬛ Faro (PS3838): {dq.pinnacle_active ? '✅ Activo' : '⏳ Sin datos'}
                </div>
              </div>
            </div>

            <button className="secondary" onClick={()=>setScraperOpen(false)} style={{width:'100%'}}>Cerrar</button>
          </div>
        </div>
      )}
    </div>
  )
}

function StatPill({ label, value, accent }) {
  const color = accent === 'yellow' ? 'var(--accent-yellow)' : accent === 'red' ? 'var(--accent-red)' : 'var(--text-muted)'
  return (
    <div className="stat-pill">
      <span className="stat-pill-label">{label}</span>
      <span className="stat-pill-val" style={{ color }}>{value}</span>
    </div>
  )
}
