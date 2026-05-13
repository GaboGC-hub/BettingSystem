import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import './index.css'
import App from './App.jsx'

try {
  const t = localStorage.getItem('evfl-theme')
  const d = localStorage.getItem('evfl-density')
  if (t === 'light' || t === 'dark') document.documentElement.setAttribute('data-theme', t)
  if (d === 'compact' || d === 'comfortable') document.documentElement.setAttribute('data-density', d)
} catch (e) {}

createRoot(document.getElementById('root')).render(
  <StrictMode>
    <App />
  </StrictMode>,
)
