import { useEffect, useMemo, useState } from 'react'
import { api, fmtDate, qs } from '../api.js'
import PageHeader from '../components/PageHeader.jsx'
import { IconSearch } from '../components/icons.jsx'

const PAGE = 50

export default function AuditLog() {
  const [items, setItems] = useState([])
  const [total, setTotal] = useState(0)
  const [actions, setActions] = useState([])
  const [err, setErr] = useState('')
  const [q, setQ] = useState('')
  const [action, setAction] = useState('')
  const [from, setFrom] = useState('')
  const [to, setTo] = useState('')
  const [page, setPage] = useState(0)

  useEffect(() => { api.get('/audit/actions').then((d) => setActions(d.items || [])).catch(() => {}) }, [])
  const filters = useMemo(() => ({ q, action, from, to, limit: PAGE, offset: page * PAGE }), [q, action, from, to, page])
  useEffect(() => {
    api.get(`/audit${qs(filters)}`).then((d) => { setItems(d.items || []); setTotal(d.total || 0) }).catch((e) => setErr(e.message))
  }, [filters])

  return (
    <div className="stack">
      <PageHeader title="Audit log" sub="Who did what: logins, configuration changes, ticket updates, and every transcript or recording view" />
      <div className="toolbar">
        <div className="search"><span className="ic"><IconSearch /></span>
          <input placeholder="User, target, detail…" value={q} onChange={(e) => { setPage(0); setQ(e.target.value) }} /></div>
        <select value={action} onChange={(e) => { setPage(0); setAction(e.target.value) }}>
          <option value="">All actions</option>
          {actions.map((a) => <option key={a} value={a}>{a}</option>)}
        </select>
        <input type="date" value={from} onChange={(e) => { setPage(0); setFrom(e.target.value) }} />
        <input type="date" value={to} onChange={(e) => { setPage(0); setTo(e.target.value) }} />
      </div>
      {err && <div className="err">{err}</div>}
      <div className="table-wrap">
        <table>
          <thead><tr><th className="no-sort">When</th><th className="no-sort">User</th><th className="no-sort">Action</th><th className="no-sort">Target</th><th className="no-sort">Detail</th><th className="no-sort">IP</th></tr></thead>
          <tbody>
            {!items.length ? <tr><td colSpan={6} className="empty">Nothing recorded yet.</td></tr> : items.map((a) => (
              <tr key={a.id}>
                <td>{fmtDate(a.created_at)}</td>
                <td>{a.username || <span className="muted">—</span>}</td>
                <td><span className={`pill ${a.action.includes('failed') ? 'red' : a.action.includes('viewed') || a.action.includes('played') ? 'amber' : 'src'}`}>{a.action}</span></td>
                <td style={{ fontFamily: 'var(--mono)', fontSize: '0.78rem' }}>{a.target || '—'}</td>
                <td className="muted" style={{ fontSize: '0.76rem', maxWidth: 360, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }} title={a.detail}>{a.detail || '—'}</td>
                <td className="muted" style={{ fontSize: '0.76rem' }}>{a.ip || '—'}</td>
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
