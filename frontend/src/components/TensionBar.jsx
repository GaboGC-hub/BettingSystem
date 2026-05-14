export default function TensionBar({ value }) {
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
