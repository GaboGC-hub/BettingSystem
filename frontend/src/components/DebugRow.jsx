export default function DebugRow({ label, ago, ok, na }) {
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
