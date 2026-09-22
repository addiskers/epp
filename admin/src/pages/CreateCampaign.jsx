import { useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { api, CAMPAIGN_TYPE_LABEL, CAMPAIGN_TYPE_DESC } from '../api.js'
import ContactUpload from '../components/ContactUpload.jsx'
import ContactsTable from '../components/ContactsTable.jsx'
import Modal from '../components/Modal.jsx'
import PageHeader from '../components/PageHeader.jsx'
import TicketPicker from '../components/TicketPicker.jsx'

const pad = (n) => String(n).padStart(2, '0')
const todayStr = () => { const d = new Date(); return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}` }
const nowTimeStr = () => { const d = new Date(); return `${pad(d.getHours())}:${pad(d.getMinutes())}` }
const blockDecimalKeys = (e) => { if (['.', ',', 'e', 'E', '+', '-'].includes(e.key)) e.preventDefault() }
const toWhole = (v) => { if (v === '') return ''; const n = Math.floor(Number(v)); return Number.isFinite(n) ? String(Math.max(0, n)) : '' }

export default function CreateCampaign() {
  const navigate = useNavigate()
  const [type, setType] = useState('intake')
  const [selected, setSelected] = useState(new Set())     // contact ids, or ticket ids for follow-up
  const [refreshKey, setRefreshKey] = useState(0)
  const [message, setMessage] = useState('')
  const [showStart, setShowStart] = useState(false)
  const [name, setName] = useState('')
  const [startDate, setStartDate] = useState(todayStr())
  const [startTime, setStartTime] = useState(nowTimeStr())
  const [delayH, setDelayH] = useState(4)
  const [maxDay, setMaxDay] = useState(3)
  const [days, setDays] = useState(1)
  const [callStart, setCallStart] = useState('09:00')
  const [callEnd, setCallEnd] = useState('21:00')
  const [err, setErr] = useState('')
  const [busy, setBusy] = useState(false)

  function pickType(t) { if (t !== type) { setType(t); setSelected(new Set()) } }
  function toggle(id) { setSelected((s) => { const n = new Set(s); n.has(id) ? n.delete(id) : n.add(id); return n }) }
  function toggleMany(ids, on) { setSelected((s) => { const n = new Set(s); ids.forEach((id) => on ? n.add(id) : n.delete(id)); return n }) }

  const isFollowup = type === 'followup'
  const needsMessage = type === 'announcement' && !message.trim()
  const canStart = selected.size > 0 && !needsMessage

  async function start() {
    setErr(''); setBusy(true)
    try {
      if (!name.trim()) throw new Error('Campaign name is required')
      if (!startDate || !startTime) throw new Error('Start date and time are required')
      const startAt = new Date(`${startDate}T${startTime}`)
      if (startAt.getTime() < Date.now() - 60000) throw new Error('Start time is in the past — pick the current time or later')
      const body = {
        name: name.trim(), campaign_type: type, message: message.trim(), start_at: startAt.toISOString(),
        callback_delay_hours: Number(delayH), callback_max_per_day: Number(maxDay), callback_days: Number(days),
        call_start: callStart, call_end: callEnd,
      }
      if (isFollowup) body.ticket_ids = [...selected]; else body.contact_ids = [...selected]
      const c = await api.post('/campaigns', body)
      navigate(`/campaigns/${c.id}`)
    } catch (e) { setErr(e.message) } finally { setBusy(false) }
  }

  return (
    <div className="stack" style={{ paddingBottom: 72 }}>
      <PageHeader title="Create campaign" sub="What kind of call, who to call, and when"
                  back={<Link to="/campaigns" className="muted" style={{ fontSize: '0.82rem' }}>← Campaigns</Link>} />

      <div className="panel">
        <div className="panel-head"><h3>1. What kind of call?</h3></div>
        <div style={{ display: 'grid', gap: 12, gridTemplateColumns: 'repeat(auto-fit, minmax(240px, 1fr))' }}>
          {Object.keys(CAMPAIGN_TYPE_LABEL).map((t) => (
            <button key={t} type="button" className="card clickable" onClick={() => pickType(t)}
                    style={{ textAlign: 'left', padding: 16, cursor: 'pointer', border: `1px solid ${type === t ? 'var(--green)' : 'var(--border)'}`,
                             background: type === t ? 'var(--green-soft)' : undefined, color: 'inherit', font: 'inherit' }}>
              <div style={{ fontWeight: 600, marginBottom: 6 }}>{CAMPAIGN_TYPE_LABEL[t]}</div>
              <div className="muted" style={{ fontSize: '0.8rem', lineHeight: 1.5 }}>{CAMPAIGN_TYPE_DESC[t]}</div>
            </button>
          ))}
        </div>
      </div>

      {isFollowup ? (
        <div className="panel">
          <div className="panel-head"><div><h3>2. Which tickets?</h3>
            <div className="muted" style={{ fontSize: '0.8rem', marginTop: 3 }}>The agent calls the contact number on each ticket.</div></div></div>
          <TicketPicker selected={selected} onToggle={toggle} onToggleMany={toggleMany} />
        </div>
      ) : (
        <>
          <ContactUpload step={2} onImported={() => setRefreshKey((k) => k + 1)} />
          <div className="panel">
            <div className="panel-head"><div><h3>3. Who to call</h3>
              <div className="muted" style={{ fontSize: '0.8rem', marginTop: 3 }}>Tick the contacts for this campaign. <Link to="/contacts">Manage contacts</Link></div></div></div>
            <ContactsTable selectable selected={selected} onToggle={toggle} onToggleMany={toggleMany} refreshKey={refreshKey} />
          </div>
        </>
      )}

      {type === 'announcement' && (
        <div className="panel">
          <div className="panel-head"><div><h3>4. The message</h3>
            <div className="muted" style={{ fontSize: '0.8rem', marginTop: 3 }}>Written once, read aloud in whichever language the person chooses. Keep it short and factual; the agent will not add to it.</div></div></div>
          <textarea rows={6} value={message} onChange={(e) => setMessage(e.target.value)} maxLength={4000}
                    placeholder="e.g. The Halol plant will remain closed on Monday the 22nd for maintenance. Regular shifts resume on Tuesday." />
          <div className="muted" style={{ fontSize: '0.74rem', marginTop: 4 }}>{message.length} / 4000</div>
        </div>
      )}

      <div className="fixed-bar" style={{ padding: '12px 26px',
                    background: 'rgba(11,16,22,0.94)', borderTop: '1px solid var(--border)', backdropFilter: 'blur(8px)',
                    display: 'flex', alignItems: 'center', justifyContent: 'space-between', zIndex: 30 }}>
        <span className="muted"><b style={{ color: 'var(--text)' }}>{selected.size}</b> {isFollowup ? 'ticket' : 'contact'}{selected.size === 1 ? '' : 's'} selected · {CAMPAIGN_TYPE_LABEL[type]}
          {needsMessage && <span style={{ color: 'var(--amber)', marginLeft: 10 }}>write the message first</span>}</span>
        <button className="btn" disabled={!canStart} onClick={() => { setErr(''); setShowStart(true) }}>Schedule campaign</button>
      </div>

      {showStart && (
        <Modal title="Schedule campaign" width={620} onClose={() => !busy && setShowStart(false)}
               footer={<><button className="btn ghost" disabled={busy} onClick={() => setShowStart(false)}>Cancel</button>
                         <button className="btn" disabled={busy} onClick={start}>{busy ? 'Starting…' : 'Start campaign'}</button></>}>
          {err && <div className="err" style={{ marginBottom: 12 }}>{err}</div>}
          <div className="row"><label>Campaign name</label><input value={name} onChange={(e) => setName(e.target.value)} placeholder={`${CAMPAIGN_TYPE_LABEL[type]} — ${todayStr()}`} autoFocus /></div>
          <div className="row"><label>Calling</label><input readOnly style={{ opacity: 0.7 }} value={`${selected.size} ${isFollowup ? 'tickets' : 'contacts'} · ${CAMPAIGN_TYPE_LABEL[type]}`} /></div>
          <div className="two">
            <div><label>Start date</label><input type="date" min={todayStr()} value={startDate} onChange={(e) => setStartDate(e.target.value)} /></div>
            <div><label>Start time</label><input type="time" value={startTime} onChange={(e) => setStartTime(e.target.value)} /></div>
          </div>
          <div className="row" style={{ marginTop: 14 }}><label>Retry if no answer after (hours)</label>
            <input type="number" min="0" step="1" inputMode="numeric" value={delayH} onKeyDown={blockDecimalKeys} onChange={(e) => setDelayH(toWhole(e.target.value))} /></div>
          <div className="two">
            <div><label>Attempts per day (1–10)</label><input type="number" min="1" max="10" step="1" inputMode="numeric" value={maxDay} onKeyDown={blockDecimalKeys} onChange={(e) => setMaxDay(toWhole(e.target.value))} /></div>
            <div><label>For how many days (1–10)</label><input type="number" min="1" max="10" step="1" inputMode="numeric" value={days} onKeyDown={blockDecimalKeys} onChange={(e) => setDays(toWhole(e.target.value))} /></div>
          </div>
          <div className="two" style={{ marginTop: 14 }}>
            <div><label>Call only after (IST)</label><input type="time" value={callStart} onChange={(e) => setCallStart(e.target.value)} /></div>
            <div><label>…and before (IST)</label><input type="time" value={callEnd} onChange={(e) => setCallEnd(e.target.value)} /></div>
          </div>
          <div className="muted" style={{ fontSize: '0.78rem', marginTop: 6 }}>No calls are placed outside these hours. Campaign calls share the live-call limit with the inbound helpline.</div>
        </Modal>
      )}
    </div>
  )
}
