import { useState, useCallback } from 'react'
import { MAX_FEED_ENTRIES } from '../constants'

export default function useFeed() {
  const [feed, setFeed] = useState([])
  const addEntry = useCallback((icon, msg, type = 'info') => {
    const entry = { id: Date.now() + Math.random(), icon, msg, type, ts: Date.now() / 1000 }
    setFeed(prev => [entry, ...prev].slice(0, MAX_FEED_ENTRIES))
  }, [])
  const clearFeed = useCallback(() => setFeed([]), [])
  return { feed, addEntry, clearFeed }
}
