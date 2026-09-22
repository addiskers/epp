import { useEffect, useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import { api, qs, fmtDate, minToHHMM, CAMPAIGN_TYPE_LABEL } from '../api.js'
import { refreshingStyle, useDebounced } from '../hooks.js'
import InlineLoader from '../components/Loading.jsx'
import PageHeader from '../components/PageHeader.jsx'
import { IconSearch } from '../components/icons.jsx'

const PAGE = 25
const STATUS_CLASS = { scheduled: 'amber', live: 'green', completed: 'src', cancelled: 'red' }

export default function Campaigns() {
  const [items, setItems] = useState([])
  const [total, setTotal] = useState(0)
  const [loading, setLoading] = useState(true)
  const [err, setErr] = useState('')
  const [q, setQ] = useState('')
  const [status, setStatus] = useState('')
  const [page, setPage] = useState(0)
  const [refreshKey, setRefreshKey] = useState(0)

  const dq = useDebounced(q, 300)
  const filters = useMemo(() => ({ q: dq, status, limit: PAGE, offset: page * PAGE }), [dq, status, page])
  useEffect(() => {
    let cancel = false
    setLoading(true)
    api.get(`/campaigns${qs(filters)}`)
      .then((d) => { if (!cancel) { setItems(d.items || []); setTotal(d.total || 0); setErr('') } })
      .catch((e) => { if (!cancel) setErr(e.message) })
      .finally(() => { if (!cancel) setLoading(false) })
    return () => { cancel = true }
  }, [filters, refreshKey])

  async function cancelCampaign(e, c) {
    e.preventDefault()
    if (!confirm(`Cancel campaign "${c.name}"? Pending calls will stop.`)) return
    try { await api.post(`/campaigns/${c.id}/cancel`); setRefreshKey((k) => k + 1) } catch (err) { alert(err.message) }
  }

  return (
    <div className="stack">
      <PageHeader title="Campaigns" sub="Outbound calling: intake rounds, ticket follow-ups, announcements"
                  actions={<Link to="/campaigns/new" className="btn">+ Create campaign</Link>} />
      <div className="panel">
        <div className="toolbar">
          <div className="search"><span className="ic"><IconSearch /></span>
            <input placeholder="Search campaigns…" value={q} onChange={(e) => { setPage(0); setQ(e.target.value) }} /></div>
          <InlineLoader show={loading} label="Searching…" />
          <select value={status} onChange={(e) => { setPage(0); setStatus(e.target.value) }}>
            <option value="">All statuses</option>
            {['scheduled', 'live', 'completed', 'cancelled'].map((s) => <option key={s} value={s}>{s}</option>)}
          </select>
        </div>
        {err && <div className="err">{err}</div>}
        <div className="table-wrap tall" style={refreshingStyle(loading)}>
          <table>
            <thead><tr>
              <th className="no-sort">Campaign</th><th className="no-sort">Type</th><th className="no-sort">Status</th>
              <th className="no-sort">Starts</th><th className="no-sort num">Recipients</th><th className="no-sort">Progress</th>
              <th className="no-sort">Calling hours</th><th className="no-sort">Retries</th><th className="no-sort"></th>
            </tr></thead>
            <tbody>
              {loading && !items.length ? <tr><td colSpan={9} className="empty">Loading…</td></tr>
                : !items.length ? <tr><td colSpan={9} className="empty">No campaigns yet.</td></tr>
                : items.map((c) => {
                  const p = c.progress || {}
                  const done = (p.done || 0) + (p.failed || 0) + (p.cancelled || 0)
                  return (
                    <tr key={c.id}>
                      <td><Link to={`/campaigns/${c.id}`} style={{ color: 'var(--green-2)', fontWeight: 600 }}>{c.name}</Link></td>
                      <td><span className="pill src">{CAMPAIGN_TYPE_LABEL[c.campaign_type] || c.campaign_type}</span></td>
                      <td><span className={`pill ${STATUS_CLASS[c.status] || 'amber'}`}>{c.status === 'live' && <span className="dot" />}{c.status}</span></td>
                      <td>{fmtDate(c.start_at)}</td>
                      <td className="num">{c.contact_count}</td>
                      <td style={{ fontSize: '0.78rem' }}>
                        {done}/{c.contact_count} finished{p.done ? ` · ${p.done} done` : ''}{p.failed ? ` · ${p.failed} failed` : ''}{p.calling ? ` · ${p.calling} on call` : ''}
                      </td>
                      <td>{minToHHMM(c.call_start_min)}–{minToHHMM(c.call_end_min)}</td>
                      <td style={{ fontSize: '0.78rem' }}>{c.callback_max_per_day}/day × {c.callback_days}d · every {c.callback_delay_hours}h</td>
                      <td>{(c.status === 'scheduled' || c.status === 'live')
                        ? <button className="btn danger sm" onClick={(e) => cancelCampaign(e, c)}>Cancel</button> : <span className="muted">—</span>}</td>
                    </tr>
                  )
                })}
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
    </div>
  )
}
