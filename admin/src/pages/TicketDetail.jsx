import { useCallback, useEffect, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { api, audioUrl, fmtDate, fmtDur, STATUS_LABEL, STATUSES, LANG_NAME, cap } from '../api.js'
import { useAuth } from '../auth.jsx'
import { MOOD_HELP } from '../components/CallDrawer.jsx'
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
              {t.sentiment_label && <span title={MOOD_HELP} style={{ cursor: 'help' }}
                className={`pill ${t.sentiment_label === 'negative' ? 'red' : t.sentiment_label === 'positive' ? 'green' : 'amber'}`}>
                Caller mood: {cap(t.sentiment_label)}</span>}
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

          <EditPanel t={t} isAdmin={isAdmin} user={user} depts={depts} users={users} cats={cats} onSaved={setT} />
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
    case 'corrected': return `Corrected on the call: ${e.from_value || '—'} → ${e.to_value || '—'}`
    case 'ai_analysis': return 'Post-call analysis'
    case 'note': return 'Note'
    default: return e.kind
  }
}

function Field({ label, value }) {
  return <div style={{ marginBottom: 10 }}><label>{label}</label><div style={{ fontSize: '0.88rem' }}>{value || <span className="muted">—</span>}</div></div>
}

// One form for everything an admin or a department user changes on a ticket. Every control
// edits a draft; nothing reaches the server until "Save changes", which sends only the fields
// that differ from the ticket in ONE request (all-or-nothing on the server).
function EditPanel({ t, isAdmin, user, depts, users, cats, onSaved }) {
  const base = {
    category: t.category || '',
    department_id: t.assigned_department_id ? String(t.assigned_department_id) : '',
    user_id: t.assigned_user_id ? String(t.assigned_user_id) : '',
    priority: t.priority || 'medium',
    status: t.status || 'open',
  }
  const [f, setF] = useState({ ...base, note: '' })
  const [err, setErr] = useState('')
  const [saved, setSaved] = useState(false)
  const [busy, setBusy] = useState(false)
  // A fresh ticket (after a save, or another ticket) resets the draft to it.
  useEffect(() => { setF({ ...base, note: '' }); setErr('') }, [t.ticket_id, t.updated_at]) // eslint-disable-line react-hooks/exhaustive-deps

  const changed = Object.keys(base).filter((k) => String(f[k] ?? '') !== String(base[k] ?? ''))
  const note = (f.note || '').trim()
  const dirty = changed.length > 0 || note.length > 0
  const set = (k, v) => setF((p) => ({ ...p, [k]: v }))

  // All five statuses are always listed (they are the fixed vocabulary the helpline reads out);
  // the current one is the default and any move not allowed from it is shown but disabled.
  const reopen = (t.status === 'resolved' || t.status === 'closed')
  const allowed = new Set((NEXT[t.status] || []).filter((s) => isAdmin || !(reopen && s === 'open')))
  const catType = (t.caller_type || '').toLowerCase()
  const catOptions = cats.filter((c) => (c.caller_type || '').toLowerCase() === catType)
  if (t.category && !catOptions.some((c) => c.name === t.category)) {
    catOptions.unshift({ id: 'current', name: t.category, department_name: t.assigned_department })
  }
  const assignees = isAdmin
    ? users.filter((u) => u.role === 'admin' || String(u.department_id || '') === String(f.department_id || ''))
    : [user].filter(Boolean)
  const noDeptUsers = isAdmin && f.department_id && !assignees.some((u) => u.role === 'dept_user')

  async function save() {
    const payload = {}
    for (const k of changed) payload[k] = f[k]
    if (note) payload.note = note
    setBusy(true); setErr(''); setSaved(false)
    try {
      const updated = await api.post(`/tickets/${encodeURIComponent(t.ticket_id)}/update`, payload)
      onSaved(updated)
      setSaved(true); setTimeout(() => setSaved(false), 2500)
    } catch (e) { setErr(e.message) } finally { setBusy(false) }
  }

  return (
    <div className="panel">
      <div className="panel-head"><h3>Update ticket</h3>{saved && <span className="pill green">Saved ✓</span>}</div>
      <div className="muted" style={{ fontSize: '0.78rem', marginBottom: 12 }}>
        Now: {t.category}{t.subcategory ? ` · ${t.subcategory}` : ''} · {t.assigned_department || 'Unassigned'} · {cap(t.priority)} · {STATUS_LABEL[t.status] || t.status}
      </div>
      {isAdmin && (
        <div className="row"><label>Category</label>
          <select value={f.category} onChange={(e) => set('category', e.target.value)}>
            {catOptions.map((c) => <option key={c.id} value={c.name}>{c.name}{c.department_name ? ` → ${c.department_name}` : ''}</option>)}
          </select>
          {changed.includes('category') && <div className="muted" style={{ fontSize: '0.72rem', marginTop: 4 }}>Changing the category re-routes the ticket to that category's department unless you pick one below.</div>}
        </div>
      )}
      {isAdmin && (
        <div className="row"><label>Department</label>
          <select value={f.department_id} onChange={(e) => set('department_id', e.target.value)}>
            <option value="">Unassigned</option>
            {depts.map((d) => <option key={d.id} value={String(d.id)}>{d.name}</option>)}
          </select>
        </div>
      )}
      <div className="row"><label>Assigned to</label>
        <select value={f.user_id} onChange={(e) => set('user_id', e.target.value)}>
          <option value="">Nobody yet</option>
          {assignees.map((u) => <option key={u.id} value={String(u.id)}>{u.name || u.username}{u.department_name ? ` · ${u.department_name}` : ''}</option>)}
        </select>
        {noDeptUsers && <div className="muted" style={{ fontSize: '0.72rem', marginTop: 4 }}>No department users exist for that department yet.</div>}
      </div>
      <div className="row"><label>Priority</label>
        <select value={f.priority} onChange={(e) => set('priority', e.target.value)}>
          <option value="high">High</option><option value="medium">Medium</option><option value="low">Low</option>
        </select>
      </div>
      <div className="row"><label>Status</label>
        <select value={f.status} onChange={(e) => set('status', e.target.value)}>
          {STATUSES.map((s) => (
            <option key={s} value={s} disabled={s !== t.status && !allowed.has(s)}>
              {STATUS_LABEL[s]}{s === t.status ? ' (current)' : !allowed.has(s) ? ' — not from here' : ''}
            </option>
          ))}
        </select>
        {!isAdmin && reopen && <div className="muted" style={{ fontSize: '0.72rem', marginTop: 4 }}>Only an admin can reopen a ticket.</div>}
      </div>
      <div className="row"><textarea rows={2} placeholder="Note for the timeline (optional)" value={f.note} onChange={(e) => set('note', e.target.value)} /></div>
      <div style={{ display: 'flex', gap: 8, alignItems: 'center', flexWrap: 'wrap' }}>
        <button className="btn" disabled={busy || !dirty} onClick={save}>{busy ? 'Saving…' : 'Save changes'}</button>
        {dirty && !busy && <button className="btn ghost sm" onClick={() => { setF({ ...base, note: '' }); setErr('') }}>Discard</button>}
        {!dirty && !saved && <span className="muted" style={{ fontSize: '0.74rem' }}>Change something above, then save.</span>}
      </div>
      {err && <div className="err" style={{ marginTop: 10 }}>{err}</div>}
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
