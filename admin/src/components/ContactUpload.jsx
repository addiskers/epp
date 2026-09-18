import { useRef, useState } from 'react'
import { uploadFile, downloadFile } from '../api.js'
import { IconDownload } from './icons.jsx'

// "Upload contacts" card — drag/drop or click, plus a sample download.
export default function ContactUpload({ onImported, step }) {
  const inputRef = useRef(null)
  const [drag, setDrag] = useState(false)
  const [busy, setBusy] = useState(false)
  const [msg, setMsg] = useState(null)
  const [err, setErr] = useState('')

  async function handleFile(file) {
    if (!file) return
    setBusy(true); setErr(''); setMsg(null)
    try {
      const r = await uploadFile('/contacts/import', file)
      setMsg(r)
      onImported?.(r)
    } catch (e) { setErr(e.message) } finally { setBusy(false); if (inputRef.current) inputRef.current.value = '' }
  }

  function onDrop(e) {
    e.preventDefault(); setDrag(false)
    const f = e.dataTransfer.files?.[0]
    if (f) handleFile(f)
  }

  return (
    <div className="card">
      <div className="panel-head">
        <h3>{step ? `${step}. ` : ''}Upload contacts</h3>
        <button className="btn ghost sm" style={{ display: 'flex', gap: 6 }}
                onClick={() => downloadFile('/contacts/template', 'contacts_template.xlsx').catch((e) => setErr(e.message))}>
          <IconDownload /> Download sample
        </button>
      </div>
      <div className={`dropzone ${drag ? 'drag' : ''}`} onClick={() => inputRef.current?.click()}
           onDragOver={(e) => { e.preventDefault(); setDrag(true) }} onDragLeave={() => setDrag(false)} onDrop={onDrop}>
        <input ref={inputRef} type="file" accept=".xls,.xlsx,.csv" hidden onChange={(e) => handleFile(e.target.files?.[0])} />
        <div className="big">{busy ? 'Uploading…' : 'Drop your .xlsx / .csv here'}</div>
        <div className="hint">or click to browse — columns: Name, Phone, Type (customer / vendor / employee), Notes</div>
      </div>
      {err && <div style={{ color: '#fca5a5', fontSize: '0.82rem', marginTop: 10 }}>{err}</div>}
      {msg && (
        <div style={{ fontSize: '0.82rem', marginTop: 12, color: 'var(--secondary)' }}>
          Read {msg.rows_read} · <b style={{ color: 'var(--green)' }}>{msg.added} added</b> · {msg.updated} updated
          {msg.invalid ? ` · ${msg.invalid} invalid` : ''}{msg.rejected ? ` · ${msg.rejected} rejected` : ''}
          {msg.unknown_headers?.length ? <div className="muted" style={{ marginTop: 4 }}>Ignored columns: {msg.unknown_headers.join(', ')}</div> : null}
        </div>
      )}
    </div>
  )
}
