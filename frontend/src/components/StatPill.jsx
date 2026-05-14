export default function StatPill({ label, value, accent }) {
  const color = accent === 'yellow' ? 'var(--accent-yellow)' : accent === 'red' ? 'var(--accent-red)' : 'var(--text-muted)'
  return (
    <div className="stat-pill">
      <span className="stat-pill-label">{label}</span>
      <span className="stat-pill-val" style={{ color }}>{value}</span>
    </div>
  )
}
