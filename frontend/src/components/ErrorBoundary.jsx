import React from 'react'

export default class ErrorBoundary extends React.Component {
  constructor(props) {
    super(props)
    this.state = { hasError: false, error: null }
  }

  static getDerivedStateFromError(error) {
    return { hasError: true, error }
  }

  componentDidCatch(error, errorInfo) {
    console.error('[ErrorBoundary]', error, errorInfo)
  }

  render() {
    if (this.state.hasError) {
      return this.props.fallback || (
        <div style={{
          padding: '1rem',
          background: 'rgba(231,76,60,0.1)',
          border: '1px solid var(--accent-red)',
          borderRadius: '8px',
          margin: '0.5rem',
        }}>
          <div style={{ color: 'var(--accent-red)', fontWeight: 700, fontSize: '0.8rem' }}>Error al renderizar</div>
          <div style={{ color: 'var(--text-dim)', fontSize: '0.65rem', marginTop: '0.3rem' }}>{this.state.error?.message}</div>
        </div>
      )
    }
    return this.props.children
  }
}
