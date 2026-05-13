import { useState, useEffect, useRef, useCallback } from 'react'
import MatchCard from './components/MatchCard'
import BankrollModal from './components/BankrollModal'
import KpiSummaryRow from './components/KpiSummaryRow'
import PnlChart from './components/PnlChart'
import RecentBetsTable from './components/RecentBetsTable'
import KellyCalculator from './components/KellyCalculator'

const API = 'http://localhost:8000/api'

const formatTime = (ts) => {
  const d = new Date(ts * 1000)
  return `${String(d.getHours()).padStart(2,'0')}:${String(d.getMinutes()).padStart(2,'0')}:${String(d.getSeconds()).padStart(2,'0')}`
}

const formatRelativeSync = (ms) => {
  if (!ms) return '—'
  const s = Math.floor((Date.now() - ms) / 1000)
  if (s < 2) return 'ahora'
  if (s < 60) return `hace ${s}s`
  const m = Math.floor(s / 60)
  if (m < 60) return `hace ${m}m`
  return `hace ${Math.floor(m / 60)}h`
}

const CRITICAL_TYPES = ['bet-signal', 'goal-event', 'red-card', 'circuit-break', 'smart-money']

function useFeed() {
  const [feed, setFeed] = useState([])
  const addEntry = useCallback((icon, msg, type = 'info') => {
    const entry = { id: Date.now() + Math.random(), icon, msg, type, ts: Date.now() / 1000 }
    setFeed(prev => [entry, ...prev].slice(0, 120))
  }, [])
  const clearFeed = useCallback(() => setFeed([]), [])
  return { feed, addEntry, clearFeed }
}

const isMacLike = typeof navigator !== 'undefined' && /Mac|iPhone|iPad|iPod/i.test(navigator.userAgent || '')

const LS_THEME = 'evfl-theme'
const LS_DENSITY = 'evfl-density'

export default function App() {
  const [matches, setMatches] = useState({})
  const [settings, setSettings] = useState({ bankroll: 1000, kelly_fraction: 0.10, preset: 'balanced', poll_seconds: 45 })
  const [newUrl, setNewUrl] = useState('')
  const [scraperUrls, setScraperUrls] = useState({ betplay: '', pinnacle: 'https://www.ps3838.com/es/sports/soccer', betano: '' })
  const [showScraperFields, setShowScraperFields] = useState(false)
  const [isBankrollOpen, setIsBankrollOpen] = useState(false)
  const [scraperMeta, setScraperMeta] = useState({})
  const [showAllFeed, setShowAllFeed] = useState(false)
  const [apiOk, setApiOk] = useState(true)
  const [lastSyncMs, setLastSyncMs] = useState(null)
  const [isRefreshing, setIsRefreshing] = useState(false)
  const [isAddingMatch, setIsAddingMatch] = useState(false)
  const [pnlHistory, setPnlHistory] = useState([])
  const [theme, setTheme] = useState(() => (typeof localStorage !== 'undefined' && localStorage.getItem(LS_THEME)) || 'dark')
  const [density, setDensity] = useState(() => (typeof localStorage !== 'undefined' && localStorage.getItem(LS_DENSITY)) || 'comfortable')
  const { feed, addEntry, clearFeed } = useFeed()
  const initialized = useRef(false)
  const urlInputRef = useRef(null)

  const hasMatches = Object.keys(matches).length > 0


  useEffect(() => {
    document.documentElement.setAttribute('data-theme', theme)
    try { localStorage.setItem(LS_THEME, theme) } catch (e) {}
  }, [theme])

  useEffect(() => {
    document.documentElement.setAttribute('data-density', density)
    try { localStorage.setItem(LS_DENSITY, density) } catch (e) {}
  }, [density])

  const allResults = Object.values(matches).filter(d => d?.result?.markets)
  const activeBets = allResults.filter(d => {
    const mkts = ['goles','corners','tarjetas']
    return mkts.some(m => {
      const dec = d.result.markets[m]?.decision
      return dec && dec.best_side !== 'NO BET' && dec.best_side !== 'PASAR'
    })
  })
  const totalExposure = allResults.reduce((sum, d) => {
    return sum + ['goles','corners','tarjetas'].reduce((s, m) => {
      const dec = d.result.markets[m]?.decision
      if (!dec || dec.best_side === 'NO BET') return s
      return s + (dec.best_stake || 0) * settings.bankroll
    }, 0)
  }, 0)

  const scraperActive = Object.values(scraperMeta).some(m => m.active)
  const scraperLastSeen = Object.values(scraperMeta).reduce((best, m) => Math.max(best, m.last_seen || 0), 0)
  const extLastSeen = Object.values(matches).reduce((best, d) => Math.max(best, d?.scraper_ts || 0), 0)
  const effectiveLastSeen = Math.max(scraperLastSeen, extLastSeen)
  const scraperAgo = effectiveLastSeen ? Math.floor((Date.now()/1000 - effectiveLastSeen)) : null
  const extActive = extLastSeen > 0 && scraperAgo !== null && scraperAgo < 30

  const detectChanges = useCallback((prev, next) => {
    if (!initialized.current) { initialized.current = true; return }
    for (const [url, d] of Object.entries(next)) {
      if (!d?.result?.markets) continue
      const p = prev[url]
      const pMkts = p?.result?.markets
      const teamName = d.snapshot?.home_team || 'Partido'

      for (const mkt of ['goles', 'corners', 'tarjetas']) {
        const cur = d.result.markets[mkt]?.decision
        const prv = pMkts?.[mkt]?.decision
        if (!cur) continue

        if (cur.best_side !== 'NO BET' && prv?.best_side === 'NO BET') {
          addEntry('🎯', `<strong>${teamName}</strong> — ${mkt.toUpperCase()} ${cur.best_side} ${cur.linea} | $${(cur.best_stake * settings.bankroll).toFixed(0)} USD`, 'bet-signal')
        }

        if (cur.raw_over && prv?.raw_over && Math.abs(cur.raw_over - prv.raw_over) > 0.15) {
          addEntry('🐋', `<strong>Smart $</strong> ${mkt.toUpperCase()}: ${prv.raw_over?.toFixed(2)} → ${cur.raw_over?.toFixed(2)}`, 'smart-money')
        }

        const note = cur?.note || ''
        if ((note.includes('Circuit') || note.includes('Kill')) && !pMkts?.[mkt]?.decision?.note?.includes('Circuit')) {
          addEntry('🛡️', `BLOQUEADO: ${note}`, 'circuit-break')
        }
      }

      const curGoals = (d.state?.goles_local||0) + (d.state?.goles_visitante||0)
      const prvGoals = (prev[url]?.state?.goles_local||0) + (prev[url]?.state?.goles_visitante||0)
      if (curGoals > prvGoals) {
        addEntry('⚽', `<strong>GOL</strong> — ${teamName} (${d.state?.goles_local}-${d.state?.goles_visitante})`, 'goal-event')
      }

      const curReds = d.state?.rojas || 0
      const prvReds = prev[url]?.state?.rojas || 0
      if (curReds > prvReds) {
        addEntry('🟥', `<strong>TARJETA ROJA</strong> — ${teamName}`, 'red-card')
      }
    }
  }, [addEntry, settings.bankroll])

  const fetchMatches = useCallback(async () => {
    try {
      const res = await fetch(`${API}/matches`)
      if (!res.ok) throw new Error('HTTP')
      const data = await res.json()
      setApiOk(true)
      setLastSyncMs(Date.now())
      setMatches(prev => {
        detectChanges(prev, data)
        return data
      })
    } catch (e) {
      setApiOk(false)
    }
  }, [detectChanges])

  const fetchPnlHistory = useCallback(async () => {
    try {
      const res = await fetch(`${API}/history?limit=200`)
      if (res.ok) {
        const data = await res.json()
        const bets = data.bets || data || []
        const normalized = bets
          .filter(b => b.profit !== undefined)
          .map(b => ({
            ts: b.ts ? b.ts * 1000 : Date.now(),
            profit: b.profit || 0,
            stake: b.stake || 0,
          }))
        setPnlHistory(normalized)
      }
    } catch { /* silencioso */ }
  }, [])

  useEffect(() => {
    fetchSettings()
    fetchMatches()
    fetchPnlHistory()
    const interval = setInterval(fetchMatches, 2500)
    const histInterval = setInterval(fetchPnlHistory, 60000)
    return () => { clearInterval(interval); clearInterval(histInterval) }
  }, [fetchMatches, fetchPnlHistory])

  useEffect(() => {
    const onKey = (e) => {
      if ((e.metaKey || e.ctrlKey) && e.key === 'k') {
        e.preventDefault()
        urlInputRef.current?.focus()
        urlInputRef.current?.select()
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [])

  const fetchSettings = async () => {
    try {
      const res = await fetch(`${API}/settings`)
      setSettings(await res.json())
    } catch (e) { setApiOk(false) }
  }

  const updateSettings = async (s) => {
    try {
      await fetch(`${API}/settings`, { method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(s) })
      setSettings(s)
    } catch (e) {}
  }

  const handleAddMatch = async (e) => {
    e.preventDefault()
    if (!newUrl.trim() || isAddingMatch) return
    setIsAddingMatch(true)
    try {
      await fetch(`${API}/matches`, { method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({ url: newUrl.trim() }) })
      addEntry('📡', `Monitoreando: ${newUrl.trim().split('#')[0].split('/').slice(-2).join('/')}`, 'info')

      const hasScraperUrls = Object.values(scraperUrls).some(v => v.trim())
      if (hasScraperUrls) {
        try {
          await fetch(`${API}/matches/scraper`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ url: newUrl.trim(), scrapers: scraperUrls })
          })
          handleScraperAttached(newUrl.trim(), scraperUrls)
          addEntry('🕷️', `Scrapers lanzados automáticamente → [${Object.keys(scraperUrls).filter(k => scraperUrls[k]).join(', ')}]`, 'scraper')
        } catch (se) {
          addEntry('⚠️', 'Partido agregado, pero scrapers fallaron al iniciar.', 'circuit-break')
        }
      }

      setNewUrl('')
      fetchMatches()
    } catch (err) {
      addEntry('⚠️', 'No se pudo agregar el partido (¿API arriba?)', 'circuit-break')
    } finally {
      setIsAddingMatch(false)
    }
  }

  const handleManualRefresh = async () => {
    setIsRefreshing(true)
    await fetchMatches()
    setTimeout(() => setIsRefreshing(false), 400)
  }

  const handleRemoveMatch = async (url) => {
    try {
      await fetch(`${API}/matches?url=${encodeURIComponent(url)}`, { method:'DELETE' })
      setScraperMeta(prev => { const n = {...prev}; delete n[url]; return n })
      fetchMatches()
    } catch (e) {}
  }

  const handleScraperAttached = (url, sources) => {
    setScraperMeta(prev => ({ ...prev, [url]: { active: true, last_seen: Date.now()/1000, sources } }))
    const src = Object.keys(sources).filter(k => sources[k]).join(', ')
    addEntry('🕷️', `Scraper anclado → [${src}]`, 'scraper')
  }

  const handleScraperUpdate = (url) => {
    setScraperMeta(prev => ({ ...prev, [url]: { ...(prev[url] || {}), last_seen: Date.now()/1000, active: true } }))
  }

  let scraperPillClass = 'idle', scraperPillLabel = 'Scrapers: OFF'
  if (extActive) {
    scraperPillClass = 'active'; scraperPillLabel = `📡 Extensión: LIVE (${scraperAgo}s)`
  } else if (scraperActive) {
    if (scraperAgo !== null && scraperAgo < 30) { scraperPillClass = 'active'; scraperPillLabel = `Scrapers: LIVE (${scraperAgo}s)` }
    else { scraperPillClass = 'error'; scraperPillLabel = `Scrapers: Sin datos (${scraperAgo}s)` }
  }

  const displayFeed = showAllFeed ? feed : feed.filter(e => CRITICAL_TYPES.includes(e.type))
  const secondaryCount = feed.filter(e => !CRITICAL_TYPES.includes(e.type)).length

  const renderUrlBar = () => (
    <div className={`url-bar ${newUrl && !newUrl.includes('sofascore') ? 'invalid' : ''}`}>
      <div className="url-bar-inner">
        <span className="url-bar-icon" aria-hidden>🔗</span>
        <form onSubmit={handleAddMatch} className="url-bar-form">
          <input
            ref={urlInputRef}
            type="url" inputMode="url" autoComplete="off" spellCheck={false}
            placeholder="Pegá un enlace de SofaScore (partido en vivo)…"
            value={newUrl}
            onChange={e => setNewUrl(e.target.value)}
            aria-label="URL del partido SofaScore"
            disabled={isAddingMatch}
          />
          <button type="button" className={`url-scraper-toggle ${showScraperFields ? 'active' : ''}`} onClick={() => setShowScraperFields(s => !s)} title="Agregar URLs de scraper">
            🕷 {showScraperFields ? '▲' : '▼'}
          </button>
          <button type="submit" className="url-bar-submit" disabled={isAddingMatch || !newUrl.trim()}>
            {isAddingMatch ? 'Agregando...' : 'Agregar partido'}
          </button>
        </form>
        <kbd className="url-bar-hint" title={isMacLike ? 'Enfocar URL (⌘K)' : 'Enfocar URL (Ctrl+K)'}>{isMacLike ? '⌘K' : 'Ctrl+K'}</kbd>
      </div>

      {showScraperFields && (
        <div className="url-scraper-panel">
          <div className="url-scraper-grid">
            {[
              { key: 'betplay',  label: '🔵 Betplay (Kambi)', placeholder: 'https://betplay.com.co/apuestas#/...' },
              { key: 'pinnacle', label: '⬛ Pinnacle / PS3838', placeholder: 'https://www.ps3838.com/es/sports/soccer' },
              { key: 'betano',   label: '🟠 Betano', placeholder: 'https://www.betano.com/live/...' },
            ].map(({ key, label, placeholder }) => (
              <div key={key} className="url-scraper-field">
                <label className="url-scraper-label">{label}</label>
                <input type="text" autoComplete="off" spellCheck={false} placeholder={placeholder}
                  value={scraperUrls[key]} onChange={e => setScraperUrls(prev => ({ ...prev, [key]: e.target.value }))}
                  className="url-scraper-input" />
              </div>
            ))}
          </div>
          <p className="url-scraper-hint">Si agregas URLs aquí, el scraper se lanza automáticamente junto con el partido.</p>
        </div>
      )}
    </div>
  )

  return (
    <div className="app-shell">
      {/* ── TOPBAR ── */}
      <header className="topbar">
        <div className="topbar-brand">
          <span className="topbar-brand-icon" aria-hidden>
            <svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor" stroke="none">
              <path d="M12 2L22 7V17L12 22L2 17V7L12 2Z"/>
            </svg>
          </span>
          <div>
            <div className="topbar-brand-text">EV Futbol</div>
            <div className="topbar-brand-sub">Live Analytics</div>
          </div>
        </div>

        <div className="topbar-kpi">
          <div className={`connection-pill ${apiOk ? 'ok' : 'err'}`}>
            <span className="connection-dot" />
            {apiOk ? 'API' : 'OFF'}
          </div>
          <div className="kpi-item kpi-green" title="Última sincronización">
            <div className="kpi-label">Sync</div>
            <div className="kpi-value mono muted">{formatRelativeSync(lastSyncMs)}</div>
          </div>
          <div className="kpi-item kpi-gold">
            <div className="kpi-label">Banca</div>
            <div className="kpi-value gold mono">${settings.bankroll?.toFixed(0)}</div>
          </div>
          <div className="kpi-item kpi-cyan">
            <div className="kpi-label">En riesgo</div>
            <div className={`kpi-value mono ${totalExposure > 0 ? 'cyan' : 'muted'}`}>${totalExposure.toFixed(2)}</div>
          </div>
          <div className="kpi-item">
            <div className="kpi-label">Partidos</div>
            <div className="kpi-value mono cyan">{Object.keys(matches).length}</div>
          </div>
          <div className="kpi-item">
            <div className="kpi-label">Señales</div>
            <div className={`kpi-value mono ${activeBets.length > 0 ? 'signal-active-kpi' : 'muted'}`}>
              {activeBets.length > 0 ? `🎯 ${activeBets.length}` : '—'}
            </div>
          </div>
          <div className="kpi-item">
            <div className="kpi-label">Kelly</div>
            <div className="kpi-value mono muted">{(settings.kelly_fraction * 100).toFixed(0)}%</div>
          </div>
        </div>

        <div className="topbar-actions">
          <div className="bankroll-pill">
            Bankroll: <strong>${settings.bankroll?.toFixed(0)}</strong>
          </div>
          <div className="live-dot-gold" title="Sistema activo" />
          <div className="topbar-view-toggles" role="group" aria-label="Apariencia">
            <button type="button" className={`view-toggle ${theme === 'dark' ? 'active' : ''}`} onClick={() => setTheme('dark')} title="Tema oscuro" aria-pressed={theme === 'dark'}>🌙</button>
            <button type="button" className={`view-toggle ${theme === 'light' ? 'active' : ''}`} onClick={() => setTheme('light')} title="Tema claro" aria-pressed={theme === 'light'}>☀️</button>
            <button type="button" className={`view-toggle ${density === 'compact' ? 'active' : ''}`} onClick={() => setDensity(d => d === 'compact' ? 'comfortable' : 'compact')} title={density === 'compact' ? 'Vista cómoda' : 'Vista compacta'} aria-pressed={density === 'compact'}>{density === 'compact' ? '▦' : '▤'}</button>
          </div>
          <div className={`scraper-pill ${scraperPillClass}`}>
            <div className={`dot ${scraperPillClass === 'active' ? 'active' : scraperPillClass === 'error' ? 'ended' : 'idle'}`}/>
            {scraperPillLabel}
          </div>
          <button type="button" className="btn-ghost xs" onClick={handleManualRefresh} disabled={isRefreshing} title="Actualizar ahora">
            {isRefreshing ? '…' : '↻'}
          </button>
          <button type="button" className="secondary xs topbar-config-btn" onClick={() => setIsBankrollOpen(true)}>⚙ Config</button>
        </div>
      </header>

      {/* ── DASHBOARD LAYOUT ── */}
      <div className="dashboard-shell">

        {/* KPI Summary Row */}
        <div className="dashboard-kpi-strip">
          <KpiSummaryRow matches={matches} settings={settings} />
        </div>

        {/* Main grid: conditionally switches layout when empty */}
        <div className={`dashboard-main ${!hasMatches ? 'is-empty' : ''}`}>

          {!hasMatches ? (
            /* CENTER STAGE (Empty state replacement) */
            <div className="dashboard-center-col">
              <div className="center-col-header">
                {renderUrlBar()}
                <p className="url-bar-help">Backend en <code className="inline-code">localhost:8000</code>. Ingresa un partido para comenzar a monitorear.</p>
              </div>
              <div className="dashboard-empty-analytics">
                <PnlChart pnlHistory={pnlHistory} />
                <RecentBetsTable matches={matches} settings={settings} />
              </div>
            </div>
          ) : (
            <>
              {/* LEFT & BOTTOM: Partidos en vivo y Analytics */}
              <div className="dashboard-matches-col">
                {renderUrlBar()}
                <div className="matches-grid">
                  {Object.entries(matches).map(([url, data]) => (
                    <MatchCard
                      key={url} url={url} data={data} settings={settings}
                      onRemove={handleRemoveMatch}
                      onScraperAttached={handleScraperAttached}
                      onScraperUpdate={handleScraperUpdate}
                      scraperMeta={scraperMeta[url]}
                    />
                  ))}
                </div>
                
                {/* BOTTOM: Analytics panels (Moved here to avoid squeezing the UI) */}
                <div className="dashboard-bottom-analytics">
                  <div className="analytics-chart">
                    <PnlChart pnlHistory={pnlHistory} />
                  </div>
                  <div className="analytics-table">
                    <RecentBetsTable matches={matches} settings={settings} />
                  </div>
                  <div className="analytics-kelly">
                    <KellyCalculator settings={settings} />
                  </div>
                </div>
              </div>
            </>
          )}

          {/* RIGHT: Alertas / Terminal */}
          <aside className="sidebar">
            <div className="sidebar-header">
              <div className="sidebar-header-left">
                <span className="sidebar-accent" aria-hidden>▍</span>
                <span>Alertas</span>
                <span className="feed-critical-badge">{feed.filter(e => CRITICAL_TYPES.includes(e.type)).length} críticos</span>
              </div>
              <div className="sidebar-header-actions">
                {feed.length > 0 && (
                  <button type="button" className="feed-clear-btn" onClick={() => clearFeed()}>Limpiar</button>
                )}
                <button type="button" className="feed-toggle-btn" onClick={() => setShowAllFeed(v => !v)}>
                  {showAllFeed ? 'Solo críticos' : `Todo (${feed.length})`}
                </button>
              </div>
            </div>

            <div className="sidebar-feed">
              {displayFeed.length === 0 ? (
                <div className="sidebar-feed-empty proactive-empty">
                  <div className="proactive-icon">📡</div>
                  <div className="proactive-title">Sistema Listo</div>
                  <div className="proactive-text">Esperando señales críticas.</div>
                  <ul className="proactive-legend">
                    <li><span className="dot a-green"></span> Value Bet</li>
                    <li><span className="dot a-gold"></span> Gol</li>
                    <li><span className="dot a-red"></span> Tarjeta / Kill Switch</li>
                    <li><span className="dot a-cyan"></span> Smart Money</li>
                  </ul>
                </div>
              ) : (
                displayFeed.map(entry => {
                  const dotClass = entry.type === 'bet-signal' ? 'a-green'
                    : entry.type === 'goal-event' ? 'a-gold'
                    : entry.type === 'red-card' ? 'a-red'
                    : entry.type === 'smart-money' ? 'a-cyan'
                    : entry.type === 'circuit-break' ? 'a-red'
                    : 'a-blue'
                  return (
                    <div key={entry.id} className={`alert-item feed-entry ${entry.type}`}>
                      <div className={`alert-dot ${dotClass}`} />
                      <div className="alert-content">
                        <div className="alert-title" dangerouslySetInnerHTML={{ __html: entry.msg.replace(/\[([^\]]+)\]/g, '<strong>[$1]</strong>') }} />
                        <div className="alert-time">{formatTime(entry.ts)}</div>
                      </div>
                    </div>
                  )
                })
              )}
            </div>

            {!showAllFeed && secondaryCount > 0 && (
              <button type="button" className="feed-see-more" onClick={() => setShowAllFeed(true)}>
                +{secondaryCount} eventos secundarios
              </button>
            )}
          </aside>
        </div>
      </div>

      {isBankrollOpen && (
        <BankrollModal settings={settings} onSave={s => { updateSettings(s); setIsBankrollOpen(false) }} onClose={() => setIsBankrollOpen(false)} />
      )}
    </div>
  )
}
