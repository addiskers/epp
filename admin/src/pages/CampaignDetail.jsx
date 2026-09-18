import { useCallback, useEffect, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { api, qs, fmtDate, minToHHMM, CAMPAIGN_TYPE_LABEL } from '../api.js'
import CallDrawer from '../components/CallDrawer.jsx'
import PageHeader from '../components/PageHeader.jsx'
import RemarkCell from '../components/RemarkCell.jsx'
import { IconSearch } from '../components/icons.jsx'

const STATUS_CLASS = { scheduled: 'amber', live: 'green', completed: 'src', cancelled: 'red' }

export default function CampaignDetail() {
  const { id } = useParams()
  const [c, setC] = useState(null)
  const [items, setItems] = useState([])
  const [err, setErr] = useState('')
  const [q, setQ] = useState('')
  const [detail, setDetail] = useState(null)
  const [note, setNote] = useState('')

  const load = useCallback(async () => {
    try {
      const [camp, recips] = await Promise.all([api.get(`/campaigns/${id}`), api.get(`/campaigns/${id}/contacts${qs({ q })}`)])
      setC(camp); setItems(recips.items || []); setErr('')
    } catch (e) { setErr(e.message) }
  }, [id, q])
  useEffect(() => { const t = setTimeout(load, q ? 250 : 0); return () => clearTimeout(t) }, [load, q])
  useEffect(() => { const t = setInterval(load, 20000); return () => clearInterval(t) }, [load])

  async function cancelCampaign() {
    if (!confirm(`Cancel campaign "${c.name}"? Pending calls will stop.`)) return
    try { await api.post(`/campaigns/${id}/cancel`); load() } catch (e) { alert(e.message) }
  }
  async function callNow(cc) {
    if (!confirm(`Call ${cc.name || cc.phone} now?`)) return
    try { await api.post(`/campaigns/${id}/contacts/${cc.id}/retry`); load() } catch (e) { alert(e.message) }
  }
  async function cancelRetry(cc) {
    if (!confirm(`Cancel the pending call to ${cc.name || cc.phone}?`)) return
    try { await api.post(`/campaigns/${id}/contacts/${cc.id}/cancel`); load() } catch (e) { alert(e.message) }
  }
  async function openCall(cc) {
    setNote('')
    try {
      const list = await api.get(`/calls${qs({ campaign_id: id, q: cc.phone, limit: 1 })}`)
      const item = (list.items || [])[0]
      if (!item) { setNote(`${cc.name || cc.phone}: attempted ${cc.attempts || 0} time(s) — no answered call to open yet.`); return }
      setDetail({ ...item, _loading: true })
      setDetail(await api.get(`/calls/${encodeURIComponent(item.id)}`))
    } catch (e) { setNote(e.message) }
  }

  if (err && !c) return <div className="stack"><div className="err">{err}</div></div>
  if (!c) return <div className="panel"><div className="muted">Loading…</div></div>

  const p = c.progress || {}
  const stats = [['Recipients', c.contact_count], ['Pending', p.pending || 0], ['On call', p.calling || 0],
                 ['Done', p.done || 0], ['Failed', p.failed || 0], ['Cancelled', p.cancelled || 0], ['Tickets created', c.tickets_created || 0]]

  return (
    <div className="stack">
      <PageHeader
        back={<Link to="/campaigns" className="muted" style={{ fontSize: '0.82rem' }}>← Campaigns</Link>}
        title={<span style={{ display: 'inline-flex', gap: 10, alignItems: 'center', flexWrap: 'wrap' }}>{c.name}
          <span className="pill src">{CAMPAIGN_TYPE_LABEL[c.campaign_type]}</span>
          <span className={`pill ${STATUS_CLASS[c.status] || 'amber'}`}>{c.status === 'live' && <span className="dot" />}{c.status}</span></span>}
        sub={`Starts ${fmtDate(c.start_at)} · calling ${minToHHMM(c.call_start_min)}–${minToHHMM(c.call_end_min)} IST · retries ${c.callback_max_per_day}/day for ${c.callback_days} day${c.callback_days > 1 ? 's' : ''}, every ${c.callback_delay_hours}h`}
        actions={(c.status === 'scheduled' || c.status === 'live') ? <button className="btn danger" onClick={cancelCampaign}>Cancel campaign</button> : null} />
      {err && <div className="err">{err}</div>}

      <div className="grid stat-grid" style={{ gridTemplateColumns: 'repeat(auto-fit, minmax(120px, 1fr))' }}>
        {stats.map(([label, val]) => <div className="card stat" key={label}><div className="label">{label}</div><div className="value" style={{ fontSize: '1.35rem' }}>{val}</div></div>)}
      </div>

      {!!Object.keys(c.outcomes || {}).length && (
        <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
          {Object.entries(c.outcomes).map(([k, n]) => <span key={k} className="pill src">{c.outcome_labels?.[k] || k}: {n}</span>)}
        </div>
      )}

      {c.campaign_type === 'announcement' && (
        <div className="panel"><div className="panel-head"><h3>The message</h3></div>
          <p style={{ whiteSpace: 'pre-wrap', margin: 0, fontSize: '0.9rem', lineHeight: 1.6 }}>{c.message}</p></div>
      )}

      <div className="panel">
        <div className="panel-head">
          <h3>Recipients</h3>
          <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
            <div className="search"><span className="ic"><IconSearch /></span>
              <input placeholder="Search name / phone / ticket…" value={q} onChange={(e) => setQ(e.target.value)} /></div>
            <button className="btn ghost sm" onClick={load}>Refresh</button>
          </div>
        </div>
        {note && <div className="muted" style={{ fontSize: '0.8rem', marginBottom: 10 }}>{note}</div>}
        <div className="table-wrap">
          <table>
            <thead><tr>
              <th className="no-sort">Name</th><th className="no-sort">Phone</th>
              {c.campaign_type === 'followup' && <th className="no-sort">Ticket</th>}
              <th className="no-sort">Last attempt</th><th className="no-sort">Next due</th><th className="no-sort num">Attempts</th>
              <th className="no-sort">Status</th><th className="no-sort">Remark</th><th className="no-sort">Action</th>
            </tr></thead>
            <tbody>
              {!items.length ? <tr><td colSpan={9} className="empty">{q ? 'No recipients match.' : 'No recipients.'}</td></tr>
                : items.map((cc) => {
                  const clickable = (cc.attempts || 0) > 0
                  return (
                    <tr key={cc.id} className={clickable ? 'clickable' : ''} onClick={clickable ? () => openCall(cc) : undefined}
                        title={clickable ? "View this recipient's call" : ''}>
                      <td>{cc.name || <span className="muted">—</span>}</td>
                      <td style={{ fontFamily: 'var(--mono)' }}>{cc.phone}</td>
                      {c.campaign_type === 'followup' && <td onClick={(e) => e.stopPropagation()}>
                        {cc.ticket_id ? <Link to={`/tickets/${cc.ticket_id}`} style={{ fontFamily: 'var(--mono)', color: 'var(--green-2)' }}>{cc.ticket_id}</Link> : '—'}</td>}
                      <td>{cc.last_attempt_at ? fmtDate(cc.last_attempt_at) : <span className="muted">—</span>}</td>
                      <td>{cc.call_status === 'pending' ? (cc.next_attempt_at ? fmtDate(cc.next_attempt_at) : <span className="muted">Queued</span>) : <span className="muted">—</span>}</td>
                      <td className="num">{cc.attempts}</td>
                      <td><span className={`pill ${cc.display_variant || 'amber'}`} title={cc.last_error || undefined}>{cc.display_status || cc.call_status}</span></td>
                      <td onClick={(e) => e.stopPropagation()}>
                        <RemarkCell value={cc.remark} onSave={(v) => api.patch(`/campaigns/${id}/contacts/${cc.id}/remark`, { remark: v })} /></td>
                      <td onClick={(e) => e.stopPropagation()} style={{ display: 'flex', gap: 6 }}>
                        {['pending', 'failed', 'cancelled'].includes(cc.call_status)
                          ? <button className="btn sm" onClick={() => callNow(cc)}>Call now</button> : <span className="muted">—</span>}
                        {cc.call_status === 'pending' && <button className="btn ghost sm" onClick={() => cancelRetry(cc)}>Cancel</button>}
                      </td>
                    </tr>
                  )
                })}
            </tbody>
          </table>
        </div>
        <div className="pager"><span>{items.length} recipients</span></div>
      </div>
      {detail && <CallDrawer call={detail} onClose={() => setDetail(null)} />}
    </div>
  )
}
