import { Link } from 'react-router-dom'
import { audioUrl, fmtDate, fmtDur, LANG_NAME } from '../api.js'
import { IconPhone } from './icons.jsx'
import { PriorityPill, StatusPill } from './Pills.jsx'
import Transcript from './Transcript.jsx'

// One call, in a modal: linked tickets, post-call analysis, recording, transcript.
// `call` may carry `_loading` while the full record is still being fetched.
export default function CallDrawer({ call, onClose }) {
  if (!call) return null
  return (
    <div className="backdrop" onClick={onClose}>
      <div className="modal" style={{ maxWidth: 620, maxHeight: '85vh', overflowY: 'auto' }} onClick={(e) => e.stopPropagation()}>
        <div className="row-between">
          <h3 style={{ display: 'flex', gap: 8, alignItems: 'center' }}><IconPhone /> {call.caller || 'Call'}</h3>
          <button className="btn ghost sm" onClick={onClose}>Close</button>
        </div>
        <div className="sub">
          {fmtDate(call.started_at)} · {call.source} · {fmtDur(call.duration_seconds)} · {LANG_NAME[call.language] || call.language || '—'}
          {call.outcome ? ` · outcome: ${call.outcome}` : ''}
        </div>
        {call.outcome_note && <div className="card" style={{ marginBottom: 14, padding: 12 }}><label>Agent note</label><div style={{ fontSize: '0.84rem' }}>{call.outcome_note}</div></div>}
        {!!call.tickets?.length && (
          <div className="card" style={{ marginBottom: 14, padding: 12 }}>
            <label>Tickets from this call</label>
            {call.tickets.map((t) => (
              <div key={t.ticket_id} className="row-between" style={{ fontSize: '0.84rem', marginTop: 6 }}>
                <Link to={`/tickets/${t.ticket_id}`} style={{ fontFamily: 'var(--mono)', color: 'var(--green-2)' }}>{t.ticket_id}</Link>
                <span>{t.category} · {t.assigned_department || '—'}</span>
                <span style={{ display: 'flex', gap: 6 }}><PriorityPill priority={t.priority} escalated={t.escalation_flag} /><StatusPill status={t.status} /></span>
              </div>
            ))}
          </div>
        )}
        {/* Which tools the agent actually called. A spoken "reference number" with no create_ticket
            row here is the agent inventing one; a create_ticket with FAILED is a server-side error. */}
        {!call._loading && (
          <div className="card" style={{ marginBottom: 14, padding: 12 }}>
            <label>Tool calls</label>
            {!(call.tool_calls || []).length
              ? <div className="muted" style={{ fontSize: '0.8rem' }}>None — the agent never called create_ticket or lookup_ticket on this call.</div>
              : (call.tool_calls || []).map((t, i) => {
                const r = t.result || {}
                const failed = r.ok === false || r.found === false
                return (
                  <div key={i} style={{ fontSize: '0.8rem', marginTop: 4, display: 'flex', gap: 8, alignItems: 'baseline' }}>
                    <span style={{ fontFamily: 'var(--mono)' }}>{t.name}</span>
                    <span className={`pill ${failed ? 'red' : 'green'}`}>{failed ? 'failed' : 'ok'}</span>
                    <span className="muted">
                      {r.ticket_id || ''}{r.status ? ` · ${r.status}` : ''}{r.outcome_status ? ` · ${r.outcome_status}` : ''}
                      {r.error ? ` · ${r.error}` : ''}
                    </span>
                  </div>
                )
              })}
          </div>
        )}
        {call.analysis && (
          <div className="card" style={{ marginBottom: 14, padding: 12 }}>
            <label>Post-call analysis</label>
            {call.analysis.status && call.analysis.status !== 'done' ? (
              <div style={{ fontSize: '0.82rem', color: call.analysis.status === 'failed' ? 'var(--red)' : 'var(--muted)' }}>
                {call.analysis.status === 'failed' ? 'Failed' : 'Skipped'}: {call.analysis.error}
                {call.analysis.status === 'failed' && <div className="muted" style={{ marginTop: 4 }}>No summary, and no ticket could be created from the transcript.</div>}
              </div>
            ) : (
              <>
                <div style={{ fontSize: '0.84rem' }}>{call.analysis.summary}</div>
                <div className="muted" style={{ fontSize: '0.76rem', marginTop: 4 }}>
                  {call.analysis.intent} · sentiment {call.analysis.sentiment_label} ({call.analysis.sentiment_score})
                  {call.analysis.is_status_inquiry ? ' · status inquiry' : ''}
                </div>
              </>
            )}
          </div>
        )}
        <label>Transcript</label>
        <Transcript messages={call.messages || call.transcript} loading={call._loading}
                    audioSrc={call.has_recording ? audioUrl(`/calls/${encodeURIComponent(call.id)}/audio`) : null} />
      </div>
    </div>
  )
}
