import { useState, useCallback, useEffect, useMemo } from 'react'
import { lockBet, unlockBet } from '../services/api'

export function useBetLock(decisionData, matchUrl, market) {
  const serverLock = decisionData?.bet_lock || null
  const [localLock, setLocalLock] = useState(serverLock)
  const [placing, setPlacing] = useState(false)

  const lockKey = useMemo(() => JSON.stringify(serverLock), [serverLock])
  useEffect(() => { setLocalLock(serverLock) }, [lockKey])

  const lock = localLock
  const isLocked = Boolean(lock)

  const placeLock = useCallback(async (side, linea, odds, stakeUsd) => {
    setPlacing(true)
    try {
      const d = await lockBet({ match_url: matchUrl, market, linea, side, odds, stake_usd: stakeUsd, source: 'manual' })
      if (d.status === 'ok') {
        setLocalLock({ lock_id: d.lock_id, locked_at: Date.now() / 1000, locked_ago_s: 0, expires_in_s: 5400, stake_usd: stakeUsd, odds })
      }
    } catch (e) { console.error('[BET LOCK] place failed', e) }
    finally { setPlacing(false) }
  }, [matchUrl, market])

  const releaseLock = useCallback(async () => {
    if (!lock) return
    try {
      await unlockBet(lock.lock_id)
      setLocalLock(null)
    } catch (e) { console.error('[BET LOCK] release failed', e) }
  }, [lock])

  return { lock, isLocked, placeLock, releaseLock, placing }
}
