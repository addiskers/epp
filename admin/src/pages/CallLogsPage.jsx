import { useEffect, useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import { api, audioUrl, fmtDate, fmtDur, qs, LANG_NAME } from '../api.js'
import PageHeader from '../components/PageHeader.jsx'
import { IconPhone, IconSearch } from '../components/icons.jsx'
import { PriorityPill, StatusPill } from '../components/Pills.jsx'
import Transcript from '../components/Transcript.jsx'

const PAGE = 25

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

  const filters = useMemo(() => ({ q, source, with_ticket: withTicket, from, to, limit: PAGE, offset: page * PAGE }),
    [q, source, withTicket, from, to, page])

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
      <PageHeader title="Call logs" sub="Every call the helpline answered, with its transcript, recording and tickets" />
      <div className="toolbar">
        <div className="search"><span className="ic"><IconSearch /></span>
          <input placeholder="Phone, ticket no, call id…" value={q} onChange={(e) => { setPage(0); setQ(e.target.value) }} /></div>
        <select value={source} onChange={(e) => { setPage(0); setSource(e.target.value) }}>
          <option value="">All sources</option>
          <option value="plivo_inbound">Inbound phone</option>
          <option value="plivo">Test call (phone)</option>
          <option value="browser">Test call (browser)</option>
        </select>
        <select value={withTicket} onChange={(e) => { setPage(0); setWithTicket(e.target.value) }}>
          <option value="">Ticket or not</option>
          <option value="1">Produced a ticket</option>
          <option value="0">No ticket</option>
        </select>
        <input type="date" value={from} onChange={(e) => { setPage(0); setFrom(e.target.value) }} />
        <input type="date" value={to} onChange={(e) => { setPage(0); setTo(e.target.value) }} />
        <button className="btn ghost sm" onClick={() => { setQ(''); setSource(''); setWithTicket(''); setFrom(''); setTo(''); setPage(0) }}>Reset</button>
      </div>
      {err && <div className="err">{err}</div>}

      <div className="table-wrap">
        <table>
          <thead><tr>
            <th className="no-sort">Started</th><th className="no-sort">Source</th><th className="no-sort">Caller</th>
            <th className="no-sort num">Duration</th><th className="no-sort">Language</th><th className="no-sort">Status</th>
            <th className="no-sort">Ticket</th><th className="no-sort num">Cost</th>
          </tr></thead>
          <tbody>
            {loading ? <tr><td colSpan={8} className="empty">Loading…</td></tr>
              : !items.length ? <tr><td colSpan={8} className="empty">No calls yet.</td></tr>
              : items.map((c) => (
                <tr key={c.id} className="clickable" onClick={() => open(c)}>
                  <td>{fmtDate(c.started_at)}</td>
                  <td><span className="pill src">{c.source}</span></td>
                  <td>{c.caller || '—'}{c.has_recording && <span title="Recording available" style={{ marginLeft: 6 }}>🔊</span>}</td>
                  <td className="num">{fmtDur(c.duration_seconds)}</td>
                  <td>{LANG_NAME[c.language] || c.language || '—'}</td>
                  <td><span className={`pill ${c.status === 'completed' ? 'green' : c.status === 'in_progress' ? 'blue' : 'amber'}`}>{c.status}</span></td>
                  <td onClick={(e) => e.stopPropagation()}>
                    {c.ticket_id ? <Link to={`/tickets/${c.ticket_id}`} style={{ fontFamily: 'var(--mono)', color: 'var(--green-2)' }}>{c.ticket_id}</Link>
                      : c.lookup_ticket_ids?.length ? <span className="muted" title="Status inquiry">looked up {c.lookup_ticket_ids.join(', ')}</span> : <span className="muted">—</span>}
                  </td>
                  <td className="num">{c.gemini_cost_usd != null ? `$${Number(c.gemini_cost_usd).toFixed(4)}` : '—'}</td>
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

      {detail && (
        <div className="backdrop" onClick={() => setDetail(null)}>
          <div className="modal" style={{ maxWidth: 620, maxHeight: '85vh', overflowY: 'auto' }} onClick={(e) => e.stopPropagation()}>
            <div className="row-between">
              <h3 style={{ display: 'flex', gap: 8, alignItems: 'center' }}><IconPhone /> {detail.caller || 'Call'}</h3>
              <button className="btn ghost sm" onClick={() => setDetail(null)}>Close</button>
            </div>
            <div className="sub">{fmtDate(detail.started_at)} · {detail.source} · {fmtDur(detail.duration_seconds)} · {LANG_NAME[detail.language] || detail.language || '—'}</div>
            {!!detail.tickets?.length && (
              <div className="card" style={{ marginBottom: 14, padding: 12 }}>
                <label>Tickets from this call</label>
                {detail.tickets.map((t) => (
                  <div key={t.ticket_id} className="row-between" style={{ fontSize: '0.84rem', marginTop: 6 }}>
                    <Link to={`/tickets/${t.ticket_id}`} style={{ fontFamily: 'var(--mono)', color: 'var(--green-2)' }}>{t.ticket_id}</Link>
                    <span>{t.category} · {t.assigned_department || '—'}</span>
                    <span style={{ display: 'flex', gap: 6 }}><PriorityPill priority={t.priority} escalated={t.escalation_flag} /><StatusPill status={t.status} /></span>
                  </div>
                ))}
              </div>
            )}
            {detail.analysis && (
              <div className="card" style={{ marginBottom: 14, padding: 12 }}>
                <label>Post-call analysis</label>
                <div style={{ fontSize: '0.84rem' }}>{detail.analysis.summary}</div>
                <div className="muted" style={{ fontSize: '0.76rem', marginTop: 4 }}>
                  {detail.analysis.intent} · sentiment {detail.analysis.sentiment_label} ({detail.analysis.sentiment_score})
                  {detail.analysis.is_status_inquiry ? ' · status inquiry' : ''}
                </div>
              </div>
            )}
            <label>Transcript</label>
            <Transcript messages={detail.messages || detail.transcript} loading={detail._loading}
                        audioSrc={detail.has_recording ? audioUrl(`/calls/${encodeURIComponent(detail.id)}/audio`) : null} />
          </div>
        </div>
      )}
    </div>
  )
}
