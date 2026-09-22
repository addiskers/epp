import { useEffect, useState } from 'react'

// A value that only settles after `ms` of quiet. Search boxes feed the *typed* value to the
// input and the debounced value to the fetch, so a request goes out per pause, not per key.
export function useDebounced(value, ms = 300) {
  const [v, setV] = useState(value)
  useEffect(() => {
    const t = setTimeout(() => setV(value), ms)
    return () => clearTimeout(t)
  }, [value, ms])
  return v
}

// Style for a table that is refreshing but still shows its previous rows: the rows dim
// instead of being replaced by a one-line "Loading…", so the page never jumps.
export const refreshingStyle = (loading) => ({ opacity: loading ? 0.55 : 1, transition: 'opacity .15s' })
