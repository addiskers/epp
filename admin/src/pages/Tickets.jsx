import { useEffect, useMemo, useState } from 'react'
import { Link, useNavigate, useSearchParams } from 'react-router-dom'
import { api, downloadFile, fmtDate, qs, STATUS_LABEL, CALLER_TYPES, LANG_NAME, cap } from '../api.js'
import { useAuth } from '../auth.jsx'
import { refreshingStyle, useDebounced } from '../hooks.js'
import PageHeader from '../components/PageHeader.jsx'
import { IconDownload, IconSearch } from '../components/icons.jsx'
import { PriorityPill, StatusPill, TypePill } from '../components/Pills.jsx'

const PAGE = 25

export default function Tickets() {
  const { isAdmin } = useAuth()
  const navigate = useNavigate()
  const [params, setParams] = useSearchParams()
  const [items, setItems] = useState([])
  const [total, setTotal] = useState(0)
  const [loading, setLoading] = useState(true)
  const [err, setErr] = useState('')
  const [depts, setDepts] = useState([])
  const [page, setPage] = useState(0)

  const f = {
    q: params.get('q') || '', status: params.get('status') || '', priority: params.get('priority') || '',
    department_id: params.get('department_id') || '', caller_type: params.get('caller_type') || '',
    escalated: params.get('escalated') || '', from: params.get('from') || '', to: params.get('to') || '',
    sort: params.get('sort') || 'created_at', dir: params.get('dir') || 'desc',
  }
  function set(k, v) {
    const n = new URLSearchParams(params)
    if (v) n.set(k, v); else n.delete(k)
    setParams(n, { replace: true }); setPage(0)
  }

  useEffect(() => { if (isAdmin) api.get('/departments').then((d) => setDepts(d.items || [])).catch(() => {}) }, [isAdmin])

  // The typed text updates the URL at once; the fetch waits for a pause in typing.
  const dq = useDebounced(f.q, 300)
  const query = useMemo(() => qs({ ...f, q: dq, limit: PAGE, offset: page * PAGE }), [params, dq, page]) // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    let cancel = false
    setLoading(true)
    api.get(`/tickets${query}`)
      .then((d) => { if (!cancel) { setItems(d.items || []); setTotal(d.total || 0); setErr('') } })
      .catch((e) => { if (!cancel) setErr(e.message) })
      .finally(() => { if (!cancel) setLoading(false) })
    return () => { cancel = true }
  }, [query])

  function th(key, label, extra = '') {
    const active = f.sort === key
    return (
      <th className={extra} onClick={() => { set('sort', key); set('dir', active && f.dir === 'desc' ? 'asc' : 'desc') }}>
        {label}{active && <span className="arrow">{f.dir === 'desc' ? '▾' : '▴'}</span>}
      </th>
    )
  }

  return (
    <div className="stack">
      <PageHeader title="Tickets" sub={`${total} matching`}
        actions={<button className="btn ghost sm" style={{ display: 'flex', gap: 6 }}
                         onClick={() => downloadFile(`/tickets.csv${qs(f)}`, 'tickets.csv').catch((e) => setErr(e.message))}><IconDownload /> Export CSV</button>} />

      <div className="toolbar">
        <div className="search">
          <span className="ic"><IconSearch /></span>
          <input placeholder="Ticket no, name, phone, text…" value={f.q} onChange={(e) => set('q', e.target.value)} />
        </div>
        <select value={f.status} onChange={(e) => set('status', e.target.value)}>
          <option value="">All statuses</option>
          <option value="open,under_review,escalated">Unresolved (open, under review, escalated)</option>
          {Object.entries(STATUS_LABEL).map(([k, l]) => <option key={k} value={k}>{l}</option>)}
        </select>
        <select value={f.priority} onChange={(e) => set('priority', e.target.value)}>
          <option value="">Any priority</option>
          <option value="high">High</option><option value="medium">Medium</option><option value="low">Low</option>
        </select>
        <select value={f.caller_type} onChange={(e) => set('caller_type', e.target.value)}>
          <option value="">All caller types</option>
          {CALLER_TYPES.map((c) => <option key={c} value={c}>{cap(c)}</option>)}
        </select>
        {isAdmin && (
          <select value={f.department_id} onChange={(e) => set('department_id', e.target.value)}>
            <option value="">All departments</option>
            {depts.map((d) => <option key={d.id} value={d.id}>{d.name}</option>)}
          </select>
        )}
        <label className="toggle" style={{ marginBottom: 0 }}>
          <input type="checkbox" checked={f.escalated === '1'} onChange={(e) => set('escalated', e.target.checked ? '1' : '')} /> Escalated only
        </label>
        <input type="date" value={f.from} onChange={(e) => set('from', e.target.value)} title="From" />
        <input type="date" value={f.to} onChange={(e) => set('to', e.target.value)} title="To" />
        <button className="btn ghost sm" onClick={() => { setParams({}, { replace: true }); setPage(0) }}>Reset</button>
      </div>

      {err && <div className="err">{err}</div>}

      <div className="table-wrap" style={refreshingStyle(loading)}>
        <table>
          <thead>
            <tr>
              {th('ticket_id', 'Ticket')}
              {th('created_at', 'Registered')}
              {th('caller_type', 'Type')}
              <th className="no-sort">Caller</th>
              {th('category', 'Category')}
              <th className="no-sort">Department</th>
              {th('priority', 'Priority')}
              {th('status', 'Status')}
              <th className="no-sort">Lang</th>
              <th className="no-sort">Summary</th>
            </tr>
          </thead>
          <tbody>
            {loading && !items.length ? <tr><td colSpan={10} className="empty">Loading…</td></tr>
              : !items.length ? <tr><td colSpan={10} className="empty">No tickets match.</td></tr>
              : items.map((t) => (
                <tr key={t.ticket_id} className="clickable" title="Open this ticket"
                    onClick={() => navigate(`/tickets/${t.ticket_id}`)}>
                  <td onClick={(e) => e.stopPropagation()}><Link to={`/tickets/${t.ticket_id}`} style={{ fontFamily: 'var(--mono)', fontWeight: 600, color: 'var(--green-2)' }}>{t.ticket_id}</Link></td>
                  <td>{fmtDate(t.created_at)}</td>
                  <td><TypePill type={t.caller_type} /></td>
                  <td>{t.caller_name || <span className="muted">—</span>}<div className="muted" style={{ fontSize: '0.72rem' }}>{t.contact_number}</div></td>
                  <td>{t.category}{t.subcategory && <div className="muted" style={{ fontSize: '0.72rem' }}>{t.subcategory}</div>}</td>
                  <td>{t.assigned_department || <span className="muted">Unassigned</span>}</td>
                  <td><PriorityPill priority={t.priority} escalated={t.escalation_flag} /></td>
                  <td><StatusPill status={t.status} /></td>
                  <td>{LANG_NAME[t.language] || t.language || '—'}</td>
                  <td className="muted" style={{ maxWidth: 320, fontSize: '0.78rem' }} title={t.ai_summary || t.description}>
                    {(t.ai_summary || t.description || '').slice(0, 110)}{(t.ai_summary || t.description || '').length > 110 ? '…' : ''}
                  </td>
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
