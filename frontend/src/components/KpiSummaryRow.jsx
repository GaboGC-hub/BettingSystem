import { useMemo } from 'react'

/**
 * KpiSummaryRow – fila de 5 KPI cards calculados desde los datos LIVE del backend.
 * Reemplaza los KPIs planos del topbar con la versión grande del dashboard de referencia.
 */
export default function KpiSummaryRow({ matches, settings }) {
  const stats = useMemo(() => {
    let totalBets = 0, totalWins = 0, totalEv = 0, evCount = 0
    let totalExposure = 0, activeSignals = 0

    for (const d of Object.values(matches)) {
      const mkts = d?.result?.markets
      if (!mkts) continue
      for (const mkt of ['goles', 'corners', 'tarjetas']) {
        const dec = mkts[mkt]?.decision
        if (!dec) continue
        if (dec.best_side && dec.best_side !== 'NO BET' && dec.best_side !== 'PASAR') {
          activeSignals++
          totalBets++
          const stakeUsd = (dec.best_stake || 0) * (settings?.bankroll || 1000)
          totalExposure += stakeUsd
          if (dec.best_ev) { 
            // Backward compatibility heuristic
            const netEv = dec.best_ev > 0.50 ? dec.best_ev - 1.0 : dec.best_ev;
            totalEv += netEv; 
            evCount++ 
            
            // Si EV Neto > 0 el modelo cree que es ganadora
            if (netEv > 0) totalWins++
          }
        }
      }
    }

    const winRate = totalBets > 0 ? (totalWins / totalBets * 100) : null
    const avgEv   = evCount > 0 ? ((totalEv / evCount) * 100) : null
    const activeMatches = Object.values(matches).filter(d => !d?.ended).length

    return { totalBets, winRate, avgEv, totalExposure, activeSignals, activeMatches }
  }, [matches, settings])

  const bankroll = settings?.bankroll || 1000
  const kelly    = (settings?.kelly_fraction || 0.1) * 100

  const cards = [
    {
      label: 'Bankroll',
      val: `$${bankroll.toLocaleString('es-CO')}`,
      sub: `Kelly ${kelly.toFixed(0)}%`,
      subClass: 'neu',
      accent: 'var(--gold, var(--accent-yellow))',
    },
    {
      label: 'Señales Activas',
      val: stats.activeSignals > 0 ? `🎯 ${stats.activeSignals}` : '—',
      sub: `${stats.activeMatches} partido${stats.activeMatches !== 1 ? 's' : ''} vivo${stats.activeMatches !== 1 ? 's' : ''}`,
      subClass: stats.activeSignals > 0 ? 'pos' : 'neu',
      accent: stats.activeSignals > 0 ? 'var(--accent-green)' : 'var(--text-dim)',
    },
    {
      label: 'En Riesgo',
      val: `$${stats.totalExposure.toFixed(2)}`,
      sub: stats.totalBets > 0 ? `${stats.totalBets} apuesta${stats.totalBets !== 1 ? 's' : ''}` : 'Sin apuestas',
      subClass: stats.totalExposure > 0 ? 'pos' : 'neu',
      accent: stats.totalExposure > 0 ? 'var(--accent-cyan)' : 'var(--text-dim)',
    },
    {
      label: 'EV Promedio',
      val: stats.avgEv !== null ? `${stats.avgEv > 0 ? '+' : ''}${stats.avgEv.toFixed(1)}%` : '—',
      sub: stats.avgEv !== null ? (stats.avgEv > 0 ? '↑ Valor positivo' : '↓ Valor negativo') : 'Sin datos',
      subClass: stats.avgEv !== null ? (stats.avgEv > 0 ? 'pos' : 'neg') : 'neu',
      accent: stats.avgEv !== null ? (stats.avgEv > 0 ? 'var(--accent-green)' : 'var(--accent-red)') : 'var(--text-dim)',
    },
    {
      label: 'Win Rate (modelo)',
      val: stats.winRate !== null ? `${stats.winRate.toFixed(0)}%` : '—',
      sub: stats.totalBets > 0 ? `${stats.totalBets} señales evaluadas` : 'Esperando señales',
      subClass: stats.winRate !== null ? (stats.winRate >= 55 ? 'pos' : stats.winRate >= 45 ? 'neu' : 'neg') : 'neu',
      accent: stats.winRate !== null ? (stats.winRate >= 55 ? 'var(--accent-green)' : 'var(--gold, var(--accent-yellow))') : 'var(--text-dim)',
    },
  ]

  return (
    <div className="kpi-summary-row">
      {cards.map((c, i) => (
        <div key={i} className="kpi-summary-card" style={{ '--kpi-c': c.accent }}>
          <div className="kpi-summary-label">{c.label}</div>
          <div className="kpi-summary-val">{c.val}</div>
          <div className={`kpi-summary-sub ${c.subClass}`}>{c.sub}</div>
        </div>
      ))}
    </div>
  )
}
