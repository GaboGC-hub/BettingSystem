import { useState, useEffect, useRef } from 'react'

/**
 * PnlChart – gráfico de curva P&L acumulada usando Canvas (Chart.js via CDN).
 * Lee los datos reales del endpoint /api/history o construye la curva
 * desde los snapshots en memoria que trae App.jsx.
 */
function buildChartOnCanvas(canvas, labels, data) {
  if (!canvas || !window.Chart) return null

  // Destruir instancia previa si existe
  const existing = window.Chart.getChart(canvas)
  if (existing) existing.destroy()

  const gradient = canvas.getContext('2d').createLinearGradient(0, 0, 0, 180)
  gradient.addColorStop(0, 'rgba(201,168,76,0.28)')
  gradient.addColorStop(1, 'rgba(201,168,76,0)')

  return new window.Chart(canvas, {
    type: 'line',
    data: {
      labels,
      datasets: [{
        data,
        borderColor: '#C9A84C',
        borderWidth: 2,
        pointBackgroundColor: '#C9A84C',
        pointRadius: (ctx) => ctx.dataIndex === data.length - 1 ? 5 : 2,
        pointHoverRadius: 5,
        fill: true,
        backgroundColor: gradient,
        tension: 0.42,
      }],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      plugins: {
        legend: { display: false },
        tooltip: {
          backgroundColor: '#1A1E2A',
          borderColor: '#C9A84C',
          borderWidth: 1,
          titleColor: '#8A8FA0',
          bodyColor: '#F0EDE6',
          padding: 10,
          callbacks: {
            label: (c) => ` ${c.raw >= 0 ? '+' : ''}$${c.raw.toFixed(2)}`,
          },
        },
      },
      scales: {
        x: {
          grid: { color: 'rgba(201,168,76,0.06)' },
          ticks: { color: '#4A5060', font: { size: 9, family: "'Space Mono', monospace" }, maxTicksLimit: 8 },
        },
        y: {
          grid: { color: 'rgba(201,168,76,0.06)' },
          ticks: {
            color: '#4A5060',
            font: { size: 9, family: "'Space Mono', monospace" },
            callback: (v) => `$${v.toFixed(0)}`,
          },
        },
      },
    },
  })
}

const PERIODS = ['Hoy', '7D', '30D', 'Todo']

export default function PnlChart({ pnlHistory }) {
  const canvasRef = useRef(null)
  const chartRef  = useRef(null)
  const [period, setPeriod]   = useState('Hoy')
  const [chartJsReady, setChartJsReady] = useState(!!window.Chart)

  // Cargar Chart.js desde CDN si no está disponible
  useEffect(() => {
    if (window.Chart) { setChartJsReady(true); return }
    const script = document.createElement('script')
    script.src = 'https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.js'
    script.onload = () => setChartJsReady(true)
    document.head.appendChild(script)
  }, [])

  // Construir/actualizar gráfico cuando los datos o el período cambian
  useEffect(() => {
    if (!chartJsReady || !canvasRef.current) return
    const { labels, data } = filterByPeriod(pnlHistory, period)
    chartRef.current = buildChartOnCanvas(canvasRef.current, labels, data)
    return () => { if (chartRef.current) chartRef.current.destroy() }
  }, [chartJsReady, pnlHistory, period])

  const { totalPnl, roi } = useMemo_pnl(pnlHistory)

  return (
    <div className="dash-panel pnl-chart-panel">
      <div className="dash-panel-header">
        <div style={{ display: 'flex', alignItems: 'center', gap: '0.75rem' }}>
          <span className="dash-panel-title">Curva P&amp;L</span>
          <span className={`tag ${totalPnl >= 0 ? 'tag-green' : 'tag-red'}`}>
            {totalPnl >= 0 ? '+' : ''}${totalPnl.toFixed(2)}
          </span>
        </div>
        <div style={{ display: 'flex', gap: '4px' }}>
          {PERIODS.map(p => (
            <button
              key={p}
              className={`ctrl-btn ${period === p ? 'active' : ''}`}
              onClick={() => setPeriod(p)}
            >{p}</button>
          ))}
        </div>
      </div>
      <div className="pnl-chart-body">
        {!chartJsReady ? (
          <div className="pnl-loading">Cargando gráfico…</div>
        ) : (
          <canvas ref={canvasRef} style={{ width: '100%', height: '180px' }} />
        )}
      </div>
    </div>
  )
}

function filterByPeriod(history, period) {
  if (!history || history.length === 0) {
    return { labels: ['—'], data: [0] }
  }
  const now = Date.now()
  const cutoffs = { 'Hoy': 86400000, '7D': 7 * 86400000, '30D': 30 * 86400000, 'Todo': Infinity }
  const cutoff = cutoffs[period] ?? Infinity
  const filtered = history.filter(h => (now - h.ts) <= cutoff)
  const src = (filtered.length > 0 ? filtered : history).slice().reverse()

  // Acumular P&L
  let cumPnl = 0
  const labels = [], data = []
  for (const h of src) {
    cumPnl += h.profit || 0
    const d = new Date(h.ts)
    labels.push(`${String(d.getDate()).padStart(2,'0')}/${String(d.getMonth()+1).padStart(2,'0')} ${String(d.getHours()).padStart(2,'0')}:${String(d.getMinutes()).padStart(2,'0')}`)
    data.push(parseFloat(cumPnl.toFixed(2)))
  }
  return { labels, data }
}

function useMemo_pnl(history) {
  if (!history || history.length === 0) return { totalPnl: 0, roi: 0 }
  const totalPnl = history.reduce((s, h) => s + (h.profit || 0), 0)
  const totalStake = history.reduce((s, h) => s + Math.abs(h.stake || 0), 0)
  const roi = totalStake > 0 ? (totalPnl / totalStake) * 100 : 0
  return { totalPnl, roi }
}
