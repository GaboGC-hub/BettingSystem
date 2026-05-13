import React, { useState } from 'react'

// ─── Top-down pitch heatmap, SofaScore style ──────────────────────────────
// 4 columnas × 3 filas = 12 zonas (vista cenital)
// Columnas: zona defensiva, mediocampo propio, mediocampo contrario, zona ofensiva
// Filas: banda izquierda, centro, banda derecha

function computeZones12(state, side) {
  const isHome = side === 'home'

  const shots      = isHome ? (state.tiros_local || 0)              : (state.tiros_visitante || 0)
  const onTarget   = isHome ? (state.tiros_puerta_local || 0)        : (state.tiros_puerta_visitante || 0)
  const crosses    = isHome ? (state.centros_local || 0)             : (state.centros_visitante || 0)
  const corners    = isHome ? (state.corners_local || 0)             : (state.corners_visitante || 0)
  const xg         = isHome ? (state.xg_local || 0)                 : (state.xg_visitante || 0)
  const tib        = isHome ? (state.touches_in_box_home || 0)      : (state.touches_in_box_away || 0)
  const poss       = isHome ? (state.posesion_local || 50)          : (100 - (state.posesion_local || 50))
  const fouls      = isHome ? (state.faltas_visitante || 0)          : (state.faltas_local || 0)
  const recentShot = state.tiros_recientes || 0
  
  const normShots    = Math.min(shots / 15.0, 1.0)
  const normOnTarget = Math.min(onTarget / 6.0, 1.0)
  const normCross    = Math.min(crosses / 15.0, 1.0)
  const normCorner   = Math.min(corners / 8.0, 1.0)
  const normXg       = Math.min(xg / 2.5, 1.0)
  const normTib      = Math.min(tib / 25.0, 1.0)
  const possRatio    = poss / 100.0

  const deepPress = possRatio * 0.5 + normShots * 0.5

  const z = [
    // col 0           col 1             col 2                                 col 3
    possRatio * 0.3,   possRatio * 0.4,  deepPress * 0.4 + normCross * 0.2,    normCross * 0.6 + normCorner * 0.4,
    possRatio * 0.4,   possRatio * 0.5,  deepPress * 0.6 + normShots * 0.3,    normTib * 0.4 + normXg * 0.4 + normOnTarget * 0.4,
    possRatio * 0.3,   possRatio * 0.4,  deepPress * 0.4 + normCross * 0.2,    normCross * 0.6 + normCorner * 0.4,
  ]

  const normRecentShot = Math.min(recentShot / 3.0, 1.0)
  z[3]  += normRecentShot * 0.2
  z[7]  += normRecentShot * 0.3
  z[11] += normRecentShot * 0.2

  const normFouls = Math.min(fouls / 10.0, 1.0)
  z[2] += normFouls * 0.2; z[6] += normFouls * 0.3; z[10] += normFouls * 0.2

  return z.map(v => Math.min(1.0, v))
}

// ─── SVG: Campo cenital 4×3 con zonas de intensidad ─────────────────────
function PitchTopDown({ zones12, color, teamName, touchesReal }) {
  // Layout: 4 columnas × 3 filas visibles en el SVG
  // SVG: 320 × 200 px lógicos
  const W = 320, H = 200
  const cols = 4, rows = 3
  const zW = W / cols         // 80px por columna
  const zH = H / rows         // ~66px por fila

  const isHome = color === 'cyan'
  const [r, g, b] = isHome ? [6, 182, 212] : [249, 115, 22]

  return (
    <div style={{ position: 'relative', maxWidth: '430px', margin: '0.5rem auto' }}>
      <svg
        viewBox={`0 0 ${W} ${H}`}
        width="100%"
        height="auto"
        style={{ display: 'block', borderRadius: '4px', overflow: 'hidden', background: 'var(--bg-void)' }}
      >
        <g>
          {/* ── Zonas de intensidad ── */}
          {zones12.map((intensity, i) => {
            const col = i % cols
            const row = Math.floor(i / cols)
            const x = col * zW
            const y = row * zH
            const alpha = 0.05 + intensity * 0.85
            return (
              <rect
                key={i}
                x={x} y={y} width={zW} height={zH}
                fill={`rgba(${r},${g},${b},${alpha.toFixed(2)})`}
                style={{ transition: 'fill 0.6s ease' }}
              />
            )
          })}

          {/* ── Marcas del campo (encima de las zonas) ── */}
          {/* Borde exterior */}
          <rect x="1" y="1" width={W-2} height={H-2} fill="none"
            stroke={`rgba(${r},${g},${b},0.3)`} strokeWidth="1.5" rx="2"/>

          {/* Línea de medio campo */}
          <line x1={W/2} y1="0" x2={W/2} y2={H}
            stroke={`rgba(${r},${g},${b},0.2)`} strokeWidth="1"/>

          {/* Círculo central */}
          <circle cx={W/2} cy={H/2} r="24"
            fill="none" stroke={`rgba(${r},${g},${b},0.2)`} strokeWidth="1"/>
          <circle cx={W/2} cy={H/2} r="2"
            fill={`rgba(${r},${g},${b},0.2)`}/>

          {/* Área grande derecha (atacante) */}
          <rect x={W*0.78} y={H*0.18} width={W*0.22} height={H*0.64}
            fill="none" stroke={`rgba(${r},${g},${b},0.25)`} strokeWidth="1"/>
          {/* Área pequeña derecha */}
          <rect x={W*0.91} y={H*0.33} width={W*0.09} height={H*0.34}
            fill="none" stroke={`rgba(${r},${g},${b},0.2)`} strokeWidth="0.8"/>
          {/* Portería derecha */}
          <rect x={W-3} y={H*0.4} width="3" height={H*0.2}
            fill={`rgba(${r},${g},${b},0.15)`} stroke={`rgba(${r},${g},${b},0.3)`} strokeWidth="1"/>

          {/* Área grande izquierda (defensiva) */}
          <rect x="0" y={H*0.18} width={W*0.22} height={H*0.64}
            fill="none" stroke={`rgba(${r},${g},${b},0.18)`} strokeWidth="1"/>
          {/* Área pequeña izquierda */}
          <rect x="0" y={H*0.33} width={W*0.09} height={H*0.34}
            fill="none" stroke={`rgba(${r},${g},${b},0.15)`} strokeWidth="0.8"/>
          {/* Portería izquierda */}
          <rect x="0" y={H*0.4} width="3" height={H*0.2}
            fill={`rgba(${r},${g},${b},0.1)`} stroke={`rgba(${r},${g},${b},0.2)`} strokeWidth="1"/>

          {/* Separadores de zona (líneas verticales tenues) */}
          {[1, 2, 3].map(c => (
            <line key={c} x1={c * zW} y1="0" x2={c * zW} y2={H}
              stroke={`rgba(${r},${g},${b},0.08)`} strokeWidth="0.6"/>
          ))}
          {/* Separadores de fila */}
          {[1, 2].map(rr => (
            <line key={rr} x1="0" y1={rr * zH} x2={W} y2={rr * zH}
              stroke={`rgba(${r},${g},${b},0.08)`} strokeWidth="0.6"/>
          ))}
          
          {/* ── Flechas de dirección de ataque (SofaScore style) ── */}
          {[0.5, 1.5, 2.5].map(multiplier => {
            const y = zH * multiplier
            return (
              <g key={y} stroke="rgba(11,14,17,0.7)" strokeWidth="1.2" fill="none">
                <line x1={W/2} y1={y} x2={W*0.75} y2={y} />
                <polyline points={`${W*0.71},${y-4} ${W*0.76},${y} ${W*0.71},${y+4}`} />
              </g>
            )
          })}
        </g>
      </svg>

      {/* Leyenda de intensidad */}
      <div style={{
        display: 'flex', alignItems: 'center', gap: '0.35rem',
        marginTop: '0.3rem', fontSize: '0.55rem', color: 'var(--text-dim)'
      }}>
        <div style={{
          width: '60px', height: '6px', borderRadius: '3px',
          background: `linear-gradient(to right, rgba(${r},${g},${b},0.05), rgba(${r},${g},${b},0.87))`
        }}/>
        <span>Baja → Alta concentración</span>
      </div>
    </div>
  )
}

function WpfBar({ label, value, color }) {
  const pct = Math.min(100, Math.round(value * 100))
  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: '0.4rem', fontSize: '0.68rem' }}>
      <span style={{ color: 'var(--text-dim)', width: '2.8rem', flexShrink: 0 }}>{label}</span>
      <div style={{ flex: 1, height: '4px', background: 'var(--hard-border)', borderRadius: '2px', overflow: 'hidden' }}>
        <div style={{ width: `${pct}%`, height: '100%', background: color, borderRadius: '2px', transition: 'width 0.5s ease' }} />
      </div>
      <span style={{ color, fontWeight: 700, fontFamily: 'monospace', fontSize: '0.67rem', width: '2.2rem', textAlign: 'right' }}>
        {value.toFixed(2)}
      </span>
    </div>
  )
}

export default function AttackHeatmap({ state, result, snap, lagActive, dataQuality }) {
  const [open, setOpen] = useState(false)
  const [activeTeam, setActiveTeam] = useState('home') // toggle como SofaScore

  if (!state) return null

  // ── 📡 Telemetría Espejo (via backend ModelResult) ──
  // Los campos wpf_home, siege_home etc. viven directamente en result (asdict del ModelResult)
  const touchesHome = state.touches_in_box_home || 0
  const touchesAway = state.touches_in_box_away || 0
  const bigMissed   = (state.big_chances_missed_home || 0) + (state.big_chances_missed_away || 0)
  
  const globalWpf = (result?.wpf_home || 0) + (result?.wpf_away || 0)
  const globalSiege = (result?.siege_home || 0) + (result?.siege_away || 0)
  
  const activeWpf = activeTeam === 'home' ? (result?.wpf_home || 0) : (result?.wpf_away || 0)
  const activeSiege = activeTeam === 'home' ? (result?.siege_home || 0) : (result?.siege_away || 0)
  
  // Normalizar Siege per-team en vez de global (techo bajado a 1.5)
  const activeSiegeNorm = Math.min(1.0, activeSiege / 1.5)
  const globalSiegeNorm = Math.min(1.0, globalSiege / 3.0)

  const wpfColor   = activeWpf > 0.35 ? 'var(--accent-cyan)' : activeWpf > 0.15 ? 'var(--accent-yellow)' : 'var(--text-dim)'
  const siegeColor = activeSiegeNorm > 0.6 ? '#f97316' : activeSiegeNorm > 0.3 ? 'var(--accent-yellow)' : 'var(--text-dim)'

  const touchesReal = dataQuality?.touches_real ?? ((touchesHome + touchesAway) > 0)
  const homeTeam = snap?.home_team || result?.home_team || 'Local'
  const awayTeam = snap?.away_team || result?.away_team || 'Visitante'


  // ── Usar datos reales si SofaScore los proveyó (están en snap), si no: estimación ────────
  const realZonesHome = snap?.attack_zones_home
  const realZonesAway = snap?.attack_zones_away
  const hasRealZonesHome = Array.isArray(realZonesHome) && realZonesHome.length === 12
  const hasRealZonesAway = Array.isArray(realZonesAway) && realZonesAway.length === 12

  const zonesHome = hasRealZonesHome ? realZonesHome : computeZones12(state, 'home')
  const zonesAway = hasRealZonesAway ? realZonesAway : computeZones12(state, 'away')
  const zonesAreReal = activeTeam === 'home' ? hasRealZonesHome : hasRealZonesAway

  const activeZones = activeTeam === 'home' ? zonesHome : zonesAway
  const activeColor = activeTeam === 'home' ? 'cyan' : 'orange'
  const activeTouches = activeTeam === 'home' ? touchesHome : touchesAway

  return (
    <div className={`heatmap-panel ${lagActive ? 'lag-active' : ''}`}>
      {/* ── BARRA COMPACTA ── */}
      <div
        className="heatmap-header"
        onClick={() => setOpen(o => !o)}
        title="Click para ver el mapa de ataque"
      >
        <span style={{ fontSize: '0.68rem', fontWeight: 700, color: 'var(--text-dim)', letterSpacing: '0.06em', flexShrink: 0 }}>
          🗺 ATAQUE <span style={{fontSize:'0.5rem', color:'var(--text-muted)'}}>{zonesAreReal ? '(Real)' : '(Calc)'} v2.2</span>
        </span>
        <div style={{ flex: 1, display: 'flex', flexDirection: 'column', gap: '0.25rem', padding: '0 0.6rem' }}>
          <WpfBar label="WPF" value={globalWpf} color={globalWpf > 0.7 ? 'var(--accent-cyan)' : globalWpf > 0.3 ? 'var(--accent-yellow)' : 'var(--text-dim)'} />
          <WpfBar label="SIEGE" value={globalSiegeNorm} color={globalSiegeNorm > 0.6 ? '#f97316' : globalSiegeNorm > 0.3 ? 'var(--accent-yellow)' : 'var(--text-dim)'} />
        </div>
        <span style={{ fontSize: '0.65rem', color: 'var(--text-dim)', flexShrink: 0 }}>{open ? '▲' : '▼'}</span>
      </div>

      {/* ── MAPA CENITAL (expandido) ── */}
      {open && (
        <div className="heatmap-body">
          {/* Toggle de equipo, estilo SofaScore */}
          <div style={{ display: 'flex', justifyContent: 'center', gap: '0.5rem', marginBottom: '0.65rem' }}>
            <button
              onClick={e => { e.stopPropagation(); setActiveTeam('home') }}
              style={{
                padding: '0.2rem 0.7rem', fontSize: '0.65rem', borderRadius: '20px',
                fontWeight: 700, transition: 'all 0.2s',
                background: activeTeam === 'home' ? 'rgba(6,182,212,0.2)' : 'var(--bg-void)',
                color: activeTeam === 'home' ? 'var(--accent-cyan)' : 'var(--text-dim)',
                border: activeTeam === 'home' ? '1px solid rgba(6,182,212,0.5)' : '1px solid var(--hard-border)',
              }}
            >
              🔵 {homeTeam}
            </button>
            <button
              onClick={e => { e.stopPropagation(); setActiveTeam('away') }}
              style={{
                padding: '0.2rem 0.7rem', fontSize: '0.65rem', borderRadius: '20px',
                fontWeight: 700, transition: 'all 0.2s',
                background: activeTeam === 'away' ? 'rgba(249,115,22,0.2)' : 'var(--bg-void)',
                color: activeTeam === 'away' ? '#f97316' : 'var(--text-dim)',
                border: activeTeam === 'away' ? '1px solid rgba(249,115,22,0.5)' : '1px solid var(--hard-border)',
              }}
            >
              🟠 {awayTeam}
            </button>
          </div>

          {/* Etiquetas de dirección */}
          <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: '0.55rem', color: 'var(--text-dim)', marginBottom: '0.2rem', padding: '0 0.1rem' }}>
            <span>← Área propia</span>
            <span>Área rival →</span>
          </div>

          <PitchTopDown
            zones12={activeZones}
            color={activeColor}
            teamName={activeTeam === 'home' ? homeTeam : awayTeam}
            touchesReal={zonesAreReal}
          />

          {/* Stats contextuales */}
          <div style={{ display: 'flex', gap: '1rem', marginTop: '0.4rem', fontSize: '0.6rem', fontFamily: 'monospace', justifyContent: 'center', flexWrap: 'wrap' }}>
            <span style={{ color: 'var(--text-dim)' }}>
              TIB <strong style={{ color: activeColor === 'cyan' ? 'var(--accent-cyan)' : '#f97316' }}>
                {activeTouches}
              </strong>
            </span>
            <span style={{ color: 'var(--text-dim)' }}>WPF <strong style={{ color: wpfColor }}>{activeWpf.toFixed(2)}</strong></span>
            <span style={{ color: 'var(--text-dim)' }}>SIEGE <strong style={{ color: siegeColor }}>{activeSiegeNorm.toFixed(2)}</strong></span>
            <span style={{ color: 'var(--text-dim)' }}>BIG MISS <strong style={{ color: 'var(--text-muted)' }}>{bigMissed}</strong></span>
          </div>
        </div>
      )}
    </div>
  )
}
