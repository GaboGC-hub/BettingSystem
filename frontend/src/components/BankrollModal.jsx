import { useState } from 'react'

export default function BankrollModal({ settings, onSave, onClose }) {
  const [bankroll, setBankroll] = useState(settings.bankroll)
  const [kelly, setKelly] = useState(settings.kelly_fraction)
  const [preset, setPreset] = useState(settings.preset || 'balanced')
  const [poll, setPoll] = useState(settings.poll_seconds || 45)

  const handleSave = () => {
    onSave({ ...settings, bankroll: parseFloat(bankroll), kelly_fraction: parseFloat(kelly), preset, poll_seconds: parseInt(poll) })
  }

  return (
    <div className="modal-backdrop" onClick={e => e.target === e.currentTarget && onClose()}>
      <div className="modal-box">
        <div className="modal-title">⚙ Configuración del Bot</div>
        <div className="modal-sub">Ajusta la gestión de capital y los parámetros del modelo cuantitativo.</div>

        <div className="form-grid">
          <div className="form-field">
            <label>Banca Total ($)</label>
            <input type="number" value={bankroll} onChange={e => setBankroll(e.target.value)} />
          </div>
          <div className="form-field">
            <label>Fracción Kelly (0–1)</label>
            <input type="number" step="0.01" min="0.01" max="1" value={kelly} onChange={e => setKelly(e.target.value)} />
          </div>
          <div className="form-field">
            <label>Preset del Modelo</label>
            <select value={preset} onChange={e => setPreset(e.target.value)} style={{ width:'100%', background:'rgba(15,23,42,0.6)', border:'1px solid var(--border)', color:'var(--text-main)', padding:'0.55rem 0.9rem', borderRadius:'8px', fontFamily:'var(--font-body)', fontSize:'0.875rem' }}>
              <option value="balanced">Balanced</option>
              <option value="aggressive">Aggressive</option>
              <option value="conservative">Conservative</option>
            </select>
          </div>
          <div className="form-field">
            <label>Intervalo Poll (segundos)</label>
            <input type="number" min="10" max="120" value={poll} onChange={e => setPoll(e.target.value)} />
          </div>
        </div>

        <div style={{ display:'flex', gap:'0.75rem', marginTop:'1.5rem' }}>
          <button onClick={handleSave} style={{ flex:1 }}>Guardar</button>
          <button className="secondary" onClick={onClose} style={{ flex:1 }}>Cancelar</button>
        </div>
      </div>
    </div>
  )
}
