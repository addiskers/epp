import { useEffect, useState } from 'react'

// Inline-editable free-text note. Saves on blur / Enter; Escape reverts.
export default function RemarkCell({ value, onSave, disabled, disabledTitle, placeholder = 'Add a note…' }) {
  const [v, setV] = useState(value || '')
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState('')
  useEffect(() => { setV(value || '') }, [value])

  async function commit() {
    if (v.trim() === (value || '').trim()) return
    setBusy(true); setErr('')
    try { await onSave(v.trim()) } catch (e) { setErr(e.message); setV(value || '') } finally { setBusy(false) }
  }

  return (
    <div>
      <input value={v} placeholder={placeholder} disabled={disabled || busy} title={disabled ? disabledTitle : undefined}
             style={{ height: 30, fontSize: '0.78rem', minWidth: 160 }}
             onChange={(e) => setV(e.target.value)} onBlur={commit}
             onKeyDown={(e) => { if (e.key === 'Enter') e.target.blur(); if (e.key === 'Escape') { setV(value || ''); e.target.blur() } }} />
      {err && <div style={{ color: '#fca5a5', fontSize: '0.7rem' }}>{err}</div>}
    </div>
  )
}
