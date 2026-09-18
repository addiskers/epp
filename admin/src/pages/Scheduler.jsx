import { useCallback, useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { api, fmtDate, CAMPAIGN_TYPE_LABEL } from '../api.js'
import PageHeader from '../components/PageHeader.jsx'
import RemarkCell from '../components/RemarkCell.jsx'

// The dial loop's kill switch and its retry queue.
export default function Scheduler() {
  const [data, setData] = useState(null)
  const [err, setErr] = useState('')
  const [busy, setBusy] = useState(false)

  const load = useCallback(() => {
    api.get('/scheduler/queue').then((d) => { setData(d); setErr('') }).catch((e) => setErr(e.message))
  }, [])
  useEffect(() => { load(); const t = setInterval(load, 20000); return () => clearInterval(t) }, [load])

  async function toggle() {
    const next = !data?.scheduler_enabled
    if (!next && !confirm('Turn the scheduler OFF? No campaign calls will be placed until it is turned on again.')) return
    setBusy(true)
    try { await api.post('/scheduler/toggle', { enabled: next }); load() } catch (e) { setErr(e.message) } finally { setBusy(false) }
  }
  async function callNow(c) {
    if (!confirm(`Call ${c.name || c.phone} now?`)) return
    try { await api.post(`/campaigns/${c.campaign_id}/contacts/${c.id}/retry`); load() } catch (e) { setErr(e.message) }
  }
  async function cancelRetry(c) {
    if (!confirm(`Cancel the pending retry for ${c.name || c.phone}?`)) return
    try { await api.post(`/campaigns/${c.campaign_id}/contacts/${c.id}/cancel`); load() } catch (e) { setErr(e.message) }
  }

  const on = data?.scheduler_enabled
  const items = data?.items || []

  return (
    <div className="stack">
      <PageHeader title="Scheduler" sub="Automatic retries for campaign calls, and the switch that stops all outbound dialing"
                  actions={data && <button className={`btn ${on ? 'danger' : ''}`} disabled={busy} onClick={toggle}>{on ? 'Turn scheduler OFF' : 'Turn scheduler ON'}</button>} />
      {err && <div className="err">{err}</div>}
      {data && !on && (
        <div style={{ background: 'var(--amber-soft)', color: 'var(--amber)', border: '1px solid rgba(240,180,84,0.3)',
                      borderRadius: 'var(--radius-sm)', padding: '10px 14px', fontSize: '0.85rem', fontWeight: 600 }}>
          Scheduler is OFF — no campaign calls will be dialed until it is turned on. Inbound calls are unaffected.
        </div>
      )}
      {data && <div className="muted" style={{ fontSize: '0.8rem' }}>{data.active_campaigns} of {data.max_active_campaigns} campaign slots in use.</div>}

      <div className="panel">
        <div className="panel-head"><div><h3>Retry queue</h3>
          <div className="muted" style={{ fontSize: '0.78rem', marginTop: 2 }}>Recipients dialled at least once: pending retries first, then history.</div></div>
          <button className="btn ghost sm" onClick={load}>Refresh</button></div>
        <div className="table-wrap">
          <table>
            <thead><tr>
              <th className="no-sort">Name</th><th className="no-sort">Phone</th><th className="no-sort">Campaign</th>
              <th className="no-sort">Last attempt</th><th className="no-sort">Next due</th><th className="no-sort num">Attempts</th>
              <th className="no-sort">Status</th><th className="no-sort">Remark</th><th className="no-sort">Actions</th>
            </tr></thead>
            <tbody>
              {!data ? <tr><td colSpan={9} className="empty">Loading…</td></tr>
                : !items.length ? <tr><td colSpan={9} className="empty">Nothing dialled yet.</td></tr>
                : items.map((c) => (
                  <tr key={c.id}>
                    <td>{c.name || <span className="muted">—</span>}</td>
                    <td style={{ fontFamily: 'var(--mono)' }}>{c.phone}</td>
                    <td><Link to={`/campaigns/${c.campaign_id}`}>{c.campaign_name}</Link> <span className="muted" style={{ fontSize: '0.72rem' }}>{CAMPAIGN_TYPE_LABEL[c.campaign_type]}</span></td>
                    <td>{fmtDate(c.last_attempt_at)}</td>
                    <td>{c.call_status === 'calling' ? <span className="muted">In progress</span>
                      : c.next_attempt_at ? fmtDate(c.next_attempt_at)
                      : c.campaign_status === 'scheduled' && c.campaign_start_at ? fmtDate(c.campaign_start_at) : <span className="muted">—</span>}</td>
                    <td className="num">{c.attempts} / {(c.campaign_max_per_day || 3) * (c.campaign_days || 1)}</td>
                    <td><span className={`pill ${c.display_variant || 'amber'}`} title={c.last_error || undefined}>{c.display_status || c.call_status}</span></td>
                    <td><RemarkCell value={c.remark} onSave={(v) => api.patch(`/campaigns/${c.campaign_id}/contacts/${c.id}/remark`, { remark: v })} /></td>
                    <td style={{ display: 'flex', gap: 6 }}>
                      {['pending', 'failed', 'cancelled'].includes(c.call_status)
                        ? <button className="btn sm" onClick={() => callNow(c)}>Call now</button> : <span className="muted">—</span>}
                      {c.call_status === 'pending' && <button className="btn ghost sm" onClick={() => cancelRetry(c)}>Cancel</button>}
                    </td>
                  </tr>
                ))}
            </tbody>
          </table>
        </div>
        <div className="pager"><span>{items.length} shown</span></div>
      </div>
    </div>
  )
}
