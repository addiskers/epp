import { STATUS_LABEL, cap } from '../api.js'

export function StatusPill({ status }) {
  return <span className={`pill ${status || 'open'}`}><span className="dot" />{STATUS_LABEL[status] || status || '—'}</span>
}

export function PriorityPill({ priority, escalated }) {
  const p = priority || 'medium'
  return <span className={`pill ${p}`} title={escalated ? 'Escalation flagged' : ''}>{escalated ? '⚑ ' : ''}{cap(p)}</span>
}

export function TypePill({ type }) {
  return <span className="pill src">{cap(type) || '—'}</span>
}
