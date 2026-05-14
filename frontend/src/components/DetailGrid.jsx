export default function DetailGrid({ decisionData, currentCount, edge }) {
  const edgeColor = edge > 5 ? 'var(--accent-green)' : edge > 2 ? 'var(--accent-yellow)' : 'var(--text-dim)'
  const hasBookie = decisionData.raw_over || decisionData.raw_under
  return (
    <div className="detail-grid-inner">
      <div className="detail-row"><span className="dlbl">Actual</span><span className="dval">{currentCount}</span></div>
      <div className="detail-row"><span className="dlbl">Linea</span><span className="dval">{decisionData.linea}</span></div>
      <div className="detail-row"><span className="dlbl">Prob Over</span><span className="dval">{(decisionData.prob_over*100).toFixed(1)}%</span></div>
      <div className="detail-row"><span className="dlbl">Prob Under</span><span className="dval">{(decisionData.prob_under*100).toFixed(1)}%</span></div>
      <div className="detail-row"><span className="dlbl">Fair Over</span><span className="dval">{decisionData.fair_over?.toFixed(3)}</span></div>
      <div className="detail-row"><span className="dlbl">Fair Under</span><span className="dval">{decisionData.fair_under?.toFixed(3)}</span></div>
      {hasBookie && (
        <>
          <div className="detail-row" style={{borderTop:'1px solid var(--hard-border)',marginTop:'0.2rem',paddingTop:'0.3rem',gridColumn:'1 / -1'}}>
            <span className="dlbl" style={{color:'var(--accent-yellow)',fontWeight:700}}>
              {(() => {
                const sid = decisionData.source_id || ''
                if (sid === 'pinnacle' || sid === 'EXT_WS_PS3838') return '🟠 CUOTA BETANO'
                if (sid === 'betano') return '⚪ ESTIMADO (Soft)'
                if (sid === 'betano_dejuiced') return '🟠 BETANO (de-juiced)'
                if (sid === 'betano_snapshot') return '⚪ ESTIMADO (Base)'
                if (sid === 'betplay') return '🔵 CUOTA BETPLAY'
                return '⚪ ESTIMADO (sin cuota real)'
              })()}
            </span>
            <span className="dval" style={{color:'var(--text-dim)',fontSize:'0.6rem'}}>
              {decisionData.source_id || 'sin fuente'}
            </span>
          </div>
          <div className="detail-row">
            <span className="dlbl">Over</span>
            <span className="dval" style={{color:'var(--text-main)',fontWeight:600}}>
              {decisionData.raw_over?.toFixed(3) ?? '—'}
            </span>
          </div>
          <div className="detail-row">
            <span className="dlbl">Under</span>
            <span className="dval" style={{color:'var(--text-main)',fontWeight:600}}>
              {decisionData.raw_under?.toFixed(3) ?? '—'}
            </span>
          </div>
        </>
      )}
      {edge !== null && (
        <div className="detail-row">
          <span className="dlbl">Edge vs Pinnacle</span>
          <span className="dval" style={{color: edgeColor}}>{edge > 0 ? '+':''}{edge?.toFixed(1)}%</span>
        </div>
      )}
    </div>
  )
}
