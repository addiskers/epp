import { useEffect, useMemo, useState } from 'react'
import { api, qs, fmtDate, cap, CALLER_TYPES } from '../api.js'
import { refreshingStyle, useDebounced } from '../hooks.js'
import InlineLoader from './Loading.jsx'
import { IconSearch } from './icons.jsx'

const PAGE = 25

// The contacts pool as a table. With `selectable`, rows carry checkboxes driven by the
// parent's Set (selected / onToggle / onToggleMany) so a campaign can be built from it.
export default function ContactsTable({ selectable, selected, onToggle, onToggleMany, onTotal, refreshKey, actions }) {
  const [items, setItems] = useState([])
  const [total, setTotal] = useState(0)
  const [loading, setLoading] = useState(true)
  const [err, setErr] = useState('')
  const [q, setQ] = useState('')
  const [type, setType] = useState('')
  const [page, setPage] = useState(0)

  const dq = useDebounced(q, 300)
  const filters = useMemo(() => ({ q: dq, caller_type: type, limit: PAGE, offset: page * PAGE }), [dq, type, page])

  useEffect(() => {
    let cancel = false
    setLoading(true)
    api.get(`/contacts${qs(filters)}`)
      .then((d) => { if (!cancel) { setItems(d.items || []); setTotal(d.total || 0); onTotal?.(d.total || 0); setErr('') } })
      .catch((e) => { if (!cancel) setErr(e.message) })
      .finally(() => { if (!cancel) setLoading(false) })
    return () => { cancel = true }
  }, [filters, refreshKey]) // eslint-disable-line react-hooks/exhaustive-deps

  const pageIds = items.map((c) => c.id)
  const allOn = selectable && pageIds.length > 0 && pageIds.every((id) => selected.has(id))

  return (
    <div>
      <div className="toolbar">
        <div className="search"><span className="ic"><IconSearch /></span>
          <input placeholder="Search name / phone / notes…" value={q} onChange={(e) => { setPage(0); setQ(e.target.value) }} /></div>
        <InlineLoader show={loading} label="Searching…" />
        <select value={type} onChange={(e) => { setPage(0); setType(e.target.value) }}>
          <option value="">All types</option>
          {CALLER_TYPES.map((t) => <option key={t} value={t}>{cap(t)}</option>)}
        </select>
        {actions}
      </div>
      {err && <div className="err">{err}</div>}
      <div className="table-wrap tall" style={refreshingStyle(loading)}>
        <table>
          <thead><tr>
            {selectable && <th className="no-sort" style={{ width: 34 }}>
              <input type="checkbox" checked={allOn} onChange={(e) => onToggleMany?.(pageIds, e.target.checked)} /></th>}
            <th className="no-sort">Name</th><th className="no-sort">Phone</th><th className="no-sort">Type</th>
            <th className="no-sort">Notes</th><th className="no-sort">Status</th><th className="no-sort">Added</th>
          </tr></thead>
          <tbody>
            {loading && !items.length ? <tr><td colSpan={7} className="empty">Loading…</td></tr>
              : !items.length ? <tr><td colSpan={7} className="empty">No contacts yet. Upload a sheet or add one.</td></tr>
              : items.map((c) => (
                <tr key={c.id} className={selectable ? 'clickable' : ''} onClick={selectable ? () => onToggle?.(c.id) : undefined}>
                  {selectable && <td onClick={(e) => e.stopPropagation()}>
                    <input type="checkbox" checked={selected.has(c.id)} onChange={() => onToggle?.(c.id)} /></td>}
                  <td>{c.name || <span className="muted">—</span>}</td>
                  <td style={{ fontFamily: 'var(--mono)' }}>{c.phone}</td>
                  <td>{c.caller_type ? <span className="pill src">{cap(c.caller_type)}</span> : <span className="muted">—</span>}</td>
                  <td className="muted" style={{ fontSize: '0.78rem', maxWidth: 260, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }} title={c.notes}>{c.notes || '—'}</td>
                  <td><span className={`pill ${c.status === 'valid' ? 'valid' : 'invalid'}`}>{c.status}</span></td>
                  <td>{fmtDate(c.created_at)}</td>
                </tr>
              ))}
          </tbody>
        </table>
      </div>
      <div className="pager">
        <span>{total} total</span>
        <button disabled={page === 0} onClick={() => setPage((p) => p - 1)}>Prev</button>
        <span>Page {page + 1}</span>
        <button disabled={(page + 1) * PAGE >= total} onClick={() => setPage((p) => p + 1)}>Next</button>
      </div>
    </div>
  )
}
