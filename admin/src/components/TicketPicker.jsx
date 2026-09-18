import { useEffect, useMemo, useState } from 'react'
import { api, qs, fmtDate, STATUS_LABEL, CALLER_TYPES, cap } from '../api.js'
import { IconSearch } from './icons.jsx'
import { PriorityPill, StatusPill, TypePill } from './Pills.jsx'

// Pick the tickets a follow-up campaign will call about. Tickets without a contact number
// are shown but cannot be selected — there is nobody to dial.
export default function TicketPicker({ selected, onToggle, onToggleMany }) {
  const [items, setItems] = useState([])
  const [total, setTotal] = useState(0)
  const [depts, setDepts] = useState([])
  const [err, setErr] = useState('')
  const [q, setQ] = useState('')
  const [status, setStatus] = useState('open,under_review,escalated')
  const [dept, setDept] = useState('')
  const [type, setType] = useState('')

  useEffect(() => { api.get('/departments?active=1').then((d) => setDepts(d.items || [])).catch(() => {}) }, [])
  const filters = useMemo(() => ({ q, status, department_id: dept, caller_type: type, limit: 200 }), [q, status, dept, type])
  useEffect(() => {
    api.get(`/tickets${qs(filters)}`).then((d) => { setItems(d.items || []); setTotal(d.total || 0); setErr('') }).catch((e) => setErr(e.message))
  }, [filters])

  const dialable = items.filter((t) => t.contact_number)
  const allOn = dialable.length > 0 && dialable.every((t) => selected.has(t.ticket_id))

  return (
    <div>
      <div className="toolbar">
        <div className="search"><span className="ic"><IconSearch /></span>
          <input placeholder="Ticket no, name, phone…" value={q} onChange={(e) => setQ(e.target.value)} /></div>
        <select value={status} onChange={(e) => setStatus(e.target.value)}>
          <option value="open,under_review,escalated">Open (any)</option>
          {Object.entries(STATUS_LABEL).map(([k, l]) => <option key={k} value={k}>{l}</option>)}
          <option value="">All statuses</option>
        </select>
        <select value={dept} onChange={(e) => setDept(e.target.value)}>
          <option value="">All departments</option>
          {depts.map((d) => <option key={d.id} value={d.id}>{d.name}</option>)}
        </select>
        <select value={type} onChange={(e) => setType(e.target.value)}>
          <option value="">All caller types</option>
          {CALLER_TYPES.map((c) => <option key={c} value={c}>{cap(c)}</option>)}
        </select>
      </div>
      {err && <div className="err">{err}</div>}
      <div className="table-wrap" style={{ maxHeight: 420, overflow: 'auto' }}>
        <table>
          <thead><tr>
            <th className="no-sort" style={{ width: 34 }}>
              <input type="checkbox" checked={allOn} onChange={(e) => onToggleMany(dialable.map((t) => t.ticket_id), e.target.checked)} /></th>
            <th className="no-sort">Ticket</th><th className="no-sort">Caller</th><th className="no-sort">Phone</th>
            <th className="no-sort">Category</th><th className="no-sort">Department</th><th className="no-sort">Status</th><th className="no-sort">Registered</th>
          </tr></thead>
          <tbody>
            {!items.length ? <tr><td colSpan={8} className="empty">No tickets match.</td></tr> : items.map((t) => {
              const ok = !!t.contact_number
              return (
                <tr key={t.ticket_id} className={ok ? 'clickable' : ''} style={{ opacity: ok ? 1 : 0.5 }}
                    onClick={ok ? () => onToggle(t.ticket_id) : undefined} title={ok ? '' : 'No contact number on this ticket'}>
                  <td onClick={(e) => e.stopPropagation()}>
                    <input type="checkbox" disabled={!ok} checked={selected.has(t.ticket_id)} onChange={() => onToggle(t.ticket_id)} /></td>
                  <td style={{ fontFamily: 'var(--mono)', fontWeight: 600 }}>{t.ticket_id}</td>
                  <td>{t.caller_name || '—'} <TypePill type={t.caller_type} /></td>
                  <td style={{ fontFamily: 'var(--mono)' }}>{t.contact_number || <span className="muted">none</span>}</td>
                  <td>{t.category}</td>
                  <td>{t.assigned_department || <span className="muted">—</span>}</td>
                  <td><span style={{ display: 'flex', gap: 4 }}><PriorityPill priority={t.priority} escalated={t.escalation_flag} /><StatusPill status={t.status} /></span></td>
                  <td>{fmtDate(t.created_at)}</td>
                </tr>
              )
            })}
          </tbody>
        </table>
      </div>
      <div className="pager"><span>{total} matching · {dialable.length} with a number</span></div>
    </div>
  )
}
