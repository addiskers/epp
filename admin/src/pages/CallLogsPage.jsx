import { useEffect, useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import { api, fmtDate, fmtDur, qs, LANG_NAME } from '../api.js'
import { refreshingStyle, useDebounced } from '../hooks.js'
import InlineLoader from '../components/Loading.jsx'
import CallDrawer from '../components/CallDrawer.jsx'
import PageHeader from '../components/PageHeader.jsx'
import { IconSearch } from '../components/icons.jsx'

const PAGE = 25
const SOURCE_LABEL = { plivo_inbound: 'Inbound', plivo_campaign: 'Campaign', plivo: 'Test (phone)', browser: 'Test (browser)' }

export default function CallLogsPage() {
  const [items, setItems] = useState([])
  const [total, setTotal] = useState(0)
  const [loading, setLoading] = useState(true)
  const [err, setErr] = useState('')
  const [detail, setDetail] = useState(null)
  const [q, setQ] = useState('')
  const [source, setSource] = useState('')
  const [withTicket, setWithTicket] = useState('')
  const [from, setFrom] = useState('')
  const [to, setTo] = useState('')
  const [page, setPage] = useState(0)

  const dq = useDebounced(q, 300)
  const filters = useMemo(() => ({ q: dq, source, with_ticket: withTicket, from, to, limit: PAGE, offset: page * PAGE }),
    [dq, source, withTicket, from, to, page])

  useEffect(() => {
    let cancel = false
    setLoading(true)
    api.get(`/calls${qs(filters)}`)
      .then((d) => { if (!cancel) { setItems(d.items || []); setTotal(d.total || 0); setErr('') } })
      .catch((e) => { if (!cancel) setErr(e.message) })
      .finally(() => { if (!cancel) setLoading(false) })
    return () => { cancel = true }
  }, [filters])

  function open(c) {
    setDetail({ ...c, _loading: true })
    api.get(`/calls/${encodeURIComponent(c.id)}`).then(setDetail).catch((e) => { setErr(e.message); setDetail(null) })
  }

  return (
    <div className="stack">
      <PageHeader title="Call logs" sub="Every call the helpline answered or placed, with its transcript, recording and tickets" />
      <div className="toolbar">
        <div className="search"><span className="ic"><IconSearch /></span>
          <input placeholder="Name, phone, ticket no, call id…" value={q} onChange={(e) => { setPage(0); setQ(e.target.value) }} /></div>
        <select value={source} onChange={(e) => { setPage(0); setSource(e.target.value) }}>
          <option value="">All sources</option>
          {Object.entries(SOURCE_LABEL).map(([k, l]) => <option key={k} value={k}>{l}</option>)}
        </select>
        <select value={withTicket} onChange={(e) => { setPage(0); setWithTicket(e.target.value) }}>
          <option value="">Ticket or not</option>
          <option value="1">Produced a ticket</option>
          <option value="0">No ticket</option>
        </select>
        <input type="date" value={from} onChange={(e) => { setPage(0); setFrom(e.target.value) }} />
        <input type="date" value={to} onChange={(e) => { setPage(0); setTo(e.target.value) }} />
        <button className="btn ghost sm" onClick={() => { setQ(''); setSource(''); setWithTicket(''); setFrom(''); setTo(''); setPage(0) }}>Reset</button>
        <InlineLoader show={loading} label="Searching…" />
      </div>
      {err && <div className="err">{err}</div>}

      <div className="table-wrap tall" style={refreshingStyle(loading)}>
        <table className="cards">
          <thead><tr>
            <th className="no-sort">Started</th><th className="no-sort">Source</th><th className="no-sort">Caller</th>
            <th className="no-sort num">Duration</th><th className="no-sort">Language</th><th className="no-sort">Status</th>
            <th className="no-sort">Ticket / outcome</th>
          </tr></thead>
          <tbody>
            {loading && !items.length ? <tr><td colSpan={7} className="empty">Loading…</td></tr>
              : !items.length ? <tr><td colSpan={7} className="empty">No calls yet.</td></tr>
              : items.map((c) => (
                <tr key={c.id} className="clickable" onClick={() => open(c)}>
                  <td data-label="Started">{fmtDate(c.started_at)}</td>
                  <td data-label="Source"><span className="pill src">{SOURCE_LABEL[c.source] || c.source}</span></td>
                  <td data-label="Caller">
                    {c.caller_name || <span className="muted">Name not given</span>}
                    {c.has_recording && <span title="Recording available" style={{ marginLeft: 6 }}>🔊</span>}
                    <div className="muted" style={{ fontSize: '0.72rem', fontFamily: 'var(--mono)' }}>{c.caller || '—'}</div>
                  </td>
                  <td data-label="Duration" className="num">{fmtDur(c.duration_seconds)}</td>
                  <td data-label="Language">{LANG_NAME[c.language] || c.language || '—'}</td>
                  <td data-label="Status"><span className={`pill ${c.status === 'completed' ? 'green' : c.status === 'in_progress' ? 'blue' : 'amber'}`}>{c.status}</span></td>
                  <td data-label="Ticket" onClick={(e) => e.stopPropagation()}>
                    {c.ticket_id ? <Link to={`/tickets/${c.ticket_id}`} style={{ fontFamily: 'var(--mono)', color: 'var(--green-2)' }}>{c.ticket_id}</Link>
                      : c.outcome ? <span className="muted">{c.outcome}{c.campaign_id ? <Link to={`/campaigns/${c.campaign_id}`} style={{ marginLeft: 6 }}>campaign</Link> : null}</span>
                      : c.lookup_ticket_ids?.length ? <span className="muted" title="Status inquiry">looked up {c.lookup_ticket_ids.join(', ')}</span> : <span className="muted">—</span>}
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
      {detail && <CallDrawer call={detail} onClose={() => setDetail(null)} />}
    </div>
  )
}
