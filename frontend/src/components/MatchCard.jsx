import React, { useState } from 'react'
import AttackHeatmap from './AttackHeatmap'
import { refreshMatch, overrideStats } from '../services/api'
import MarketSignal from './MarketSignal'
import TensionBar from './TensionBar'
import DebugRow from './DebugRow'
import StatPill from './StatPill'

function MatchCard({ url, data, settings, onRemove, onScraperUpdate, scraperMeta }) {
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
  const isSafeMode = false

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
      await overrideStats(url, editStats)
      setStatsOpen(false)
    } catch { /* noop */ }
  }

  const isEnded = snap.status_text?.toLowerCase()?.includes('ended') || snap.status_text?.toLowerCase()?.includes('ft')
  const tension = result.tension_index || 0

  const goalDec   = result.markets?.goles?.decision
  const cornerDec = result.markets?.corners?.decision
  const cardDec   = result.markets?.tarjetas?.decision

  const hasSignal = [goalDec, cornerDec, cardDec].some(d => d && d.best_side !== 'NO BET' && d.best_side !== 'PASAR')
  const scraperActive = scraperMeta?.active
  const scraperAgo = scraperMeta?.last_seen ? Math.floor(Date.now()/1000 - scraperMeta.last_seen) : null

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
    <div className={`match-card ${hasSignal ? 'has-signal' : ''} ${isEnded ? 'ended' : ''} ${isRefreshing ? 'match-card--refreshing' : ''}`}>
      <TensionBar value={tension} />

      <div className="card-header">
        <div className="card-meta">
          <div className={`dot ${isEnded ? 'ended' : 'live'}`}/>
          <span>{snap.tournament}</span>
          <span className="card-meta-right">
            <span className="live-minute">{snap.minute}&apos;</span>
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

        <div className="stats-pill-row">
          <StatPill label="xG" value={`${(state.xg_local||0).toFixed(1)}–${(state.xg_visitante||0).toFixed(1)}`} />
          <StatPill label="CRN" value={state.corners} />
          <StatPill label="TAR" value={(state.amarillas||0) + (state.rojas||0)} accent={(state.amarillas||0)+(state.rojas||0) >= 4 ? 'yellow' : null}/>
          <StatPill label="FOUL" value={state.faltas} />
          {state.rojas > 0 && <StatPill label="ROJA" value={state.rojas} accent="red"/>}
          {hasWhale && <span className="whale-badge">🐋 Smart $</span>}
        </div>
      </div>

      <AttackHeatmap state={state} result={result} snap={snap} lagActive={cardDec?.note?.includes('LAG SNIPER')} dataQuality={dq}/>

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
              Uno o más mercados del Faro superan 45s sin actualizar (revisá Sensores). Kelly en 0 para esos mercados.
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

      <div className="signal-list">
        <MarketSignal title="Goles"    decisionData={goalDec}   matchUrl={url} settings={settings} currentCount={(state.goles_local||0)+(state.goles_visitante||0)} faroStale={false}/>
        <MarketSignal title="Corners"  decisionData={cornerDec} matchUrl={url} settings={settings} currentCount={state.corners||0} faroStale={false}/>
        <MarketSignal title="Tarjetas" decisionData={cardDec}   matchUrl={url} settings={settings} currentCount={(state.amarillas||0)+((state.rojas||0)*2)} faroStale={false}/>
      </div>

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
                  await refreshMatch(url)
                  onScraperUpdate?.(url)
                } catch { /* noop */ }
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

      <div style={{
        borderTop: '1px solid var(--hard-border)',
        background: 'var(--bg-void)',
      }}>
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
              <span style={{ fontFamily: 'monospace' }}>umbral 45s</span>
            </div>
            <DebugRow label="SofaScore" ago={sofaLagS} ok={sofaLagS !== null && sofaLagS < 30} />
            <DebugRow
              label="Pinnacle (Faro)"
              ago={dq.pinnacle_active ? scraperLagS : null}
              ok={true}
              na={!dq.pinnacle_active}
            />
            {dq.pinnacle_active && (
              <>
                <DebugRow label="Faro — Goles" ago={faroPulse.GOLES?.lag_s ?? null} ok={true} na={false} />
                <DebugRow label="Faro — Corners" ago={faroPulse.CORNERS?.lag_s ?? null} ok={true} na={false} />
                <DebugRow label="Faro — Tarjetas" ago={faroPulse.TARJETAS?.lag_s ?? null} ok={true} na={false} />
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

      <div className={`scraper-badge ${scraperActive ? 'live' : ''}`}>
        <div className={`dot ${scraperActive ? 'active' : 'idle'}`}/>
        {scraperActive ? `Cuotas live (+${scraperAgo}s)` : 'Scraper inactivo — clic 🕷 para inyectar URLs'}
      </div>

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

export default React.memo(MatchCard)
