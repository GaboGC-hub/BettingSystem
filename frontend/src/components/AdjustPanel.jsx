export default function AdjustPanel({ editing, setEditing, customLine, setCustomLine, customOver, setCustomOver, customUnder, setCustomUnder, simming, handleSave, simResult, decisionData, showReason, setShowReason }) {
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
          {simResult.best_side} {simResult.linea} — Prob {(simResult.best_prob*100).toFixed(1)}% — EV {simResult.best_ev !== undefined && simResult.best_ev !== null ? (simResult.best_ev > 0.50 ? ((simResult.best_ev - 1) * 100).toFixed(1) : (simResult.best_ev * 100).toFixed(1)) : '0.0'}%
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
