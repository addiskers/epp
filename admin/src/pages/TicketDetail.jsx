import { useCallback, useEffect, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { api, audioUrl, fmtDate, fmtDur, STATUS_LABEL, STATUSES, LANG_NAME, cap } from '../api.js'
import { useAuth } from '../auth.jsx'
import PageHeader from '../components/PageHeader.jsx'
import { PriorityPill, StatusPill, TypePill } from '../components/Pills.jsx'
import Transcript from '../components/Transcript.jsx'

const NEXT = {
  open: ['under_review', 'escalated', 'resolved', 'closed'],
  under_review: ['open', 'escalated', 'resolved', 'closed'],
  escalated: ['under_review', 'resolved', 'closed'],
  resolved: ['closed', 'open'],
  closed: ['open'],
}

export default function TicketDetail() {
  const { id } = useParams()
  const { isAdmin, user } = useAuth()
  const [t, setT] = useState(null)
  const [err, setErr] = useState('')
  const [depts, setDepts] = useState([])
  const [users, setUsers] = useState([])
  const [cats, setCats] = useState([])
  const [transcript, setTranscript] = useState(null)
  const [showTranscript, setShowTranscript] = useState(false)

  const load = useCallback(() => api.get(`/tickets/${encodeURIComponent(id)}`).then(setT).catch((e) => setErr(e.message)), [id])
  useEffect(() => { load() }, [load])
  useEffect(() => {
    if (!isAdmin) return
    api.get('/departments?active=1').then((d) => setDepts(d.items || [])).catch(() => {})
    api.get('/users').then((d) => setUsers((d.items || []).filter((u) => u.active))).catch(() => {})
    api.get('/categories?active=1').then((d) => setCats(d.items || [])).catch(() => {})
  }, [isAdmin])

  async function act(path, body) {
    setErr('')
    try { setT(await api.post(`/tickets/${encodeURIComponent(id)}/${path}`, body)) }
    catch (e) { setErr(e.message) }
  }

  async function loadTranscript() {
    setShowTranscript(true)
    if (transcript) return
    try { setTranscript(await api.get(`/tickets/${encodeURIComponent(id)}/transcript`)) }
    catch (e) { setErr(e.message) }
  }

  if (err && !t) return <div className="stack"><div className="err">{err}</div></div>
  if (!t) return <div className="panel"><div className="muted">Loading…</div></div>

  const deptUsers = users.filter((u) => u.role === 'dept_user' && u.department_id === t.assigned_department_id)

  return (
    <div className="stack">
      <PageHeader
        title={<span style={{ fontFamily: 'var(--mono)' }}>{t.ticket_id}</span>}
        sub={<span><TypePill type={t.caller_type} /> {t.category}{t.subcategory ? ` · ${t.subcategory}` : ''} · registered {fmtDate(t.created_at)} · {cap(t.source)}</span>}
        back={<Link to="/tickets" className="muted" style={{ fontSize: '0.82rem' }}>← All tickets</Link>}
        actions={<div style={{ display: 'flex', gap: 8 }}><PriorityPill priority={t.priority} escalated={t.escalation_flag} /><StatusPill status={t.status} /></div>}
      />
      {err && <div className="err">{err}</div>}

      <div className="split">
        <div className="stack">
          <div className="panel">
            <div className="panel-head"><h3>The concern</h3></div>
            <p style={{ whiteSpace: 'pre-wrap', fontSize: '0.9rem', lineHeight: 1.6, margin: 0 }}>{t.description}</p>
          </div>

          <div className="panel">
            <div className="panel-head"><h3>AI analysis</h3>
              {t.sentiment_label && <span className={`pill ${t.sentiment_label === 'negative' ? 'red' : t.sentiment_label === 'positive' ? 'green' : 'amber'}`}>
                {cap(t.sentiment_label)}{t.sentiment_score != null ? ` · ${Number(t.sentiment_score).toFixed(2)}` : ''}</span>}
            </div>
            {t.ai_summary ? (
              <>
                <p style={{ fontSize: '0.88rem', lineHeight: 1.6, margin: 0 }}>{t.ai_summary}</p>
                <div className="muted" style={{ fontSize: '0.78rem', marginTop: 8 }}>
                  Intent: {t.intent || '—'}{t.category_original ? ` · originally filed as ${t.category_original}` : ''}
                  {!!t.escalation_flags?.length && <> · Flags: {t.escalation_flags.join(', ')}</>}
                  {t.escalation_reason && <> · Escalation: {t.escalation_reason}</>}
                </div>
              </>
            ) : <div className="muted" style={{ fontSize: '0.82rem' }}>Not analysed yet — the summary arrives a few seconds after the call ends.</div>}
          </div>

          <div className="panel">
            <div className="panel-head"><h3>Transcript</h3>
              {!showTranscript && <button className="btn ghost sm" onClick={loadTranscript}>View transcript{t.has_recording ? ' & recording' : ''}</button>}
            </div>
            {showTranscript
              ? <Transcript messages={transcript?.messages} loading={!transcript}
                            audioSrc={t.has_recording ? audioUrl(`/tickets/${encodeURIComponent(id)}/audio`) : null} />
              : <div className="muted" style={{ fontSize: '0.8rem' }}>{t.call_id ? 'Viewing a transcript is recorded in the audit log.' : 'No call is linked to this ticket.'}</div>}
            {transcript?.call && <div className="muted" style={{ fontSize: '0.76rem', marginTop: 10 }}>
              Call {fmtDate(transcript.call.started_at)} · {fmtDur(transcript.call.duration_seconds)} · {LANG_NAME[transcript.call.language] || transcript.call.language || '—'} · {transcript.call.source}
            </div>}
          </div>

          <div className="panel">
            <div className="panel-head"><h3>Timeline</h3></div>
            <div className="stack" style={{ gap: 10 }}>
              {(t.events || []).map((e) => (
                <div key={e.id} style={{ display: 'grid', gridTemplateColumns: '150px 1fr', gap: 12, fontSize: '0.82rem' }}>
                  <div className="muted">{fmtDate(e.created_at)}<div style={{ fontSize: '0.72rem' }}>{e.actor_name || cap(e.actor_type)}</div></div>
                  <div>
                    <b>{eventLabel(e)}</b>
                    {e.note && <div className="muted" style={{ marginTop: 2, whiteSpace: 'pre-wrap' }}>{e.note}</div>}
                  </div>
                </div>
              ))}
            </div>
          </div>
        </div>

        <div className="stack">
          <div className="panel">
            <div className="panel-head"><h3>Caller</h3></div>
            <Field label="Name" value={t.caller_name} />
            <Field label="Contact" value={t.contact_number} />
            {t.caller_type === 'customer' && <Field label="Company" value={t.company_name} />}
            {t.caller_type === 'vendor' && <Field label="Vendor code" value={t.vendor_code} />}
            {t.caller_type === 'employee' && <>
              <Field label="Employee ID" value={t.employee_id} />
              <Field label="Department" value={t.caller_department} />
              <Field label="Plant / location" value={t.plant_location} />
            </>}
            <Field label="Language" value={LANG_NAME[t.language] || t.language} />
          </div>

          <div className="panel">
            <div className="panel-head"><h3>Classification &amp; assignment</h3></div>
            <Field label="Category" value={t.subcategory ? `${t.category} · ${t.subcategory}` : t.category} />
            <Field label="Department" value={t.assigned_department || 'Unassigned'} />
            {isAdmin && (
              <div className="row"><label>Move to department</label>
                <select value={t.assigned_department_id || ''} onChange={(e) => act('assign', { department_id: e.target.value ? Number(e.target.value) : '' })}>
                  <option value="">Unassigned</option>
                  {depts.map((d) => <option key={d.id} value={d.id}>{d.name}</option>)}
                </select>
              </div>
            )}
            <div className="row"><label>Assigned to</label>
              <select value={t.assigned_user_id || ''} onChange={(e) => act('assign', { user_id: e.target.value ? Number(e.target.value) : '' })}>
                <option value="">Nobody yet</option>
                {(isAdmin ? users.filter((u) => u.role === 'admin' || u.department_id === t.assigned_department_id) : [user]).map((u) => (
                  <option key={u.id} value={u.id}>{u.name || u.username}{u.department_name ? ` · ${u.department_name}` : ''}</option>
                ))}
              </select>
              {isAdmin && !deptUsers.length && t.assigned_department_id && <div className="muted" style={{ fontSize: '0.72rem', marginTop: 4 }}>No department users exist for {t.assigned_department} yet.</div>}
            </div>
            {isAdmin && (
              <div className="row"><label>Reclassify</label>
                <select value="" onChange={(e) => e.target.value && act('reclassify', { category: e.target.value })}>
                  <option value="">Change category…</option>
                  {cats.filter((c) => c.caller_type === t.caller_type && c.name !== t.category).map((c) => (
                    <option key={c.id} value={c.name}>{c.name} → {c.department_name || 'unassigned'}</option>
                  ))}
                </select>
              </div>
            )}
            <div className="row"><label>Priority</label>
              <select value={t.priority} onChange={(e) => act('priority', { priority: e.target.value })}>
                <option value="high">High</option><option value="medium">Medium</option><option value="low">Low</option>
              </select>
            </div>
          </div>

          <StatusBox t={t} isAdmin={isAdmin} onChange={(status, note) => act('status', { status, note })} />
          <NoteBox onAdd={(note) => act('note', { note })} />
        </div>
      </div>
    </div>
  )
}

function eventLabel(e) {
  switch (e.kind) {
    case 'created': return `Registered · ${e.note || ''}`
    case 'status': return `${STATUS_LABEL[e.from_value] || e.from_value} → ${STATUS_LABEL[e.to_value] || e.to_value}`
    case 'assigned': return `Department: ${e.from_value || '—'} → ${e.to_value || 'unassigned'}`
    case 'assigned_user': return `Assigned to ${e.to_value || 'nobody'}`
    case 'reclassified': return `Reclassified: ${e.from_value} → ${e.to_value}`
    case 'priority': return `Priority: ${e.from_value} → ${e.to_value}`
    case 'ai_analysis': return 'Post-call analysis'
    case 'note': return 'Note'
    default: return e.kind
  }
}

function Field({ label, value }) {
  return <div style={{ marginBottom: 10 }}><label>{label}</label><div style={{ fontSize: '0.88rem' }}>{value || <span className="muted">—</span>}</div></div>
}

function StatusBox({ t, isAdmin, onChange }) {
  const [status, setStatus] = useState('')
  const [note, setNote] = useState('')
  // All five statuses are always listed (they are the fixed vocabulary the helpline reads out);
  // the current one and any move not allowed from it are shown but disabled.
  const reopen = (t.status === 'resolved' || t.status === 'closed')
  const allowed = new Set((NEXT[t.status] || []).filter((s) => isAdmin || !(reopen && s === 'open')))
  return (
    <div className="panel">
      <div className="panel-head"><h3>Update status</h3></div>
      <div className="row">
        <select value={status} onChange={(e) => setStatus(e.target.value)}>
          <option value="">Change to…</option>
          {STATUSES.map((s) => (
            <option key={s} value={s} disabled={s === t.status || !allowed.has(s)}>
              {STATUS_LABEL[s]}{s === t.status ? ' (current)' : !allowed.has(s) ? ' — not from here' : ''}
            </option>
          ))}
        </select>
      </div>
      <div className="row"><textarea rows={2} placeholder="Note for the timeline (optional)" value={note} onChange={(e) => setNote(e.target.value)} /></div>
      <button className="btn" disabled={!status} onClick={() => { onChange(status, note); setStatus(''); setNote('') }}>Update</button>
      {!isAdmin && (t.status === 'resolved' || t.status === 'closed') && <div className="muted" style={{ fontSize: '0.72rem', marginTop: 6 }}>Only an admin can reopen a ticket.</div>}
    </div>
  )
}

function NoteBox({ onAdd }) {
  const [note, setNote] = useState('')
  return (
    <div className="panel">
      <div className="panel-head"><h3>Add a note</h3></div>
      <div className="row"><textarea rows={3} value={note} onChange={(e) => setNote(e.target.value)} placeholder="What was done, who was spoken to…" /></div>
      <button className="btn ghost" disabled={!note.trim()} onClick={() => { onAdd(note.trim()); setNote('') }}>Add note</button>
    </div>
  )
}
