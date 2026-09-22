import { useEffect, useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import { api, fmtDate, fmtDur, STATUS_LABEL, cap } from '../api.js'
import { useAuth } from '../auth.jsx'
import PageHeader from '../components/PageHeader.jsx'
import { PriorityPill, StatusPill, TypePill } from '../components/Pills.jsx'

export default function Dashboard() {
  const { isAdmin, user } = useAuth()
  const [s, setS] = useState(null)
  const [err, setErr] = useState('')

  useEffect(() => {
    let alive = true
    const load = () => api.get('/summary').then((d) => alive && setS(d)).catch((e) => alive && setErr(e.message))
    load()
    const t = setInterval(load, 30000)
    return () => { alive = false; clearInterval(t) }
  }, [])

  const t = s?.tickets
  const calls = s?.calls

  return (
    <div className="stack">
      <PageHeader title="Dashboard"
                  sub={isAdmin ? 'Tickets, calls and what needs attention' : `Tickets assigned to ${user?.department_name || 'your department'}`} />
      {err && <div className="err">{err}</div>}
      {s?.gemini_status?.error && (
        <div style={{ background: 'var(--red-soft)', border: '1px solid var(--red)', borderRadius: 'var(--radius-sm)',
                      padding: '10px 14px', fontSize: '0.85rem' }}>
          <b style={{ color: 'var(--red)' }}>The voice model is refusing calls.</b>{' '}
          {s.gemini_status.error}{' '}
          <span className="muted">(last seen {fmtDate(s.gemini_status.at)}). Callers hear silence until this is fixed —
          check the AI Studio spending cap and the API key.</span>
        </div>
      )}

      <div className="grid stat-grid">
        <Stat label="Open tickets" value={t?.open} sub={`${t?.total ?? '—'} total`} />
        <Stat label="High priority open" value={t?.high_open} sub="flagged for escalation" color={t?.high_open ? 'var(--red)' : undefined} />
        <Stat label="Registered today" value={t?.today} sub="since midnight, India time" />
        {isAdmin && <Stat label="Avg call" value={calls ? fmtDur(calls.avg_duration_seconds) : '—'}
                          sub={`${calls?.total_calls ?? 0} calls · ${Math.round((calls?.ticket_rate || 0) * 100)}% produced a ticket`} />}
      </div>

      {isAdmin && <LivePanel initial={s?.live_calls || []} />}

      <div className="grid" style={{ gridTemplateColumns: 'repeat(auto-fit, minmax(260px, 1fr))' }}>
        <Breakdown title="By status" data={t?.by_status} labels={STATUS_LABEL} order={Object.keys(STATUS_LABEL)} />
        <Breakdown title="By department" data={t?.by_department} />
        <Breakdown title="By caller type" data={t?.by_caller_type} labels={{ customer: 'Customer', vendor: 'Vendor', employee: 'Employee' }} />
        <Breakdown title="By priority" data={t?.by_priority} order={['high', 'medium', 'low']} labels={{ high: 'High', medium: 'Medium', low: 'Low' }} />
      </div>

      <div className="grid halves">
        <div className="panel">
          <div className="panel-head"><h3>Last 7 days</h3></div>
          <Bars data={t?.by_day || []} />
        </div>
        <div className="panel">
          <div className="panel-head"><h3>Escalated &amp; open</h3><Link to="/tickets?escalated=1" className="muted" style={{ fontSize: '0.8rem' }}>All →</Link></div>
          {!t?.recent_escalated?.length ? <div className="muted" style={{ fontSize: '0.85rem' }}>Nothing flagged right now.</div> : (
            <div className="stack" style={{ gap: 8 }}>
              {t.recent_escalated.map((x) => (
                <Link key={x.ticket_id} to={`/tickets/${x.ticket_id}`} className="card clickable" style={{ padding: '10px 12px' }}>
                  <div className="row-between">
                    <div style={{ fontFamily: 'var(--mono)', fontWeight: 600 }}>{x.ticket_id}</div>
                    <div style={{ display: 'flex', gap: 6 }}><PriorityPill priority={x.priority} escalated={x.escalation_flag} /><StatusPill status={x.status} /></div>
                  </div>
                  <div style={{ fontSize: '0.8rem', marginTop: 4 }}>
                    <TypePill type={x.caller_type} /> {x.category} · {x.assigned_department || 'Unassigned'} · {fmtDate(x.created_at)}
                  </div>
                  <div className="muted" style={{ fontSize: '0.78rem', marginTop: 4 }}>{x.ai_summary || x.description?.slice(0, 140)}</div>
                </Link>
              ))}
            </div>
          )}
        </div>
      </div>
    </div>
  )
}

function Stat({ label, value, sub, color }) {
  return (
    <div className="card stat">
      <div className="label">{label}</div>
      <div className="value" style={color ? { color } : undefined}>{value ?? '—'}</div>
      {sub && <div className="sub">{sub}</div>}
    </div>
  )
}

function Breakdown({ title, data, labels = {}, order }) {
  const entries = Object.entries(data || {})
  const keys = order ? order.filter((k) => k in (data || {})).concat(entries.map(([k]) => k).filter((k) => !order.includes(k))) : entries.map(([k]) => k).sort((a, b) => (data[b] - data[a]))
  const max = Math.max(1, ...entries.map(([, v]) => v))
  return (
    <div className="panel">
      <div className="panel-head"><h3>{title}</h3></div>
      {!keys.length ? <div className="muted" style={{ fontSize: '0.82rem' }}>No tickets yet.</div> : keys.map((k) => (
        <div key={k} style={{ marginBottom: 8 }}>
          <div className="row-between" style={{ fontSize: '0.8rem' }}><span>{labels[k] || cap(k) || 'Unset'}</span><span className="muted">{data[k]}</span></div>
          <div className="bar"><div className="bar-fill" style={{ width: `${(100 * data[k]) / max}%` }} /></div>
        </div>
      ))}
    </div>
  )
}

function Bars({ data }) {
  const days = []
  for (let i = 6; i >= 0; i--) {
    const d = new Date(Date.now() - i * 86400000).toISOString().slice(0, 10)
    days.push({ date: d, tickets: data.find((x) => x.date === d)?.tickets || 0 })
  }
  const max = Math.max(1, ...days.map((d) => d.tickets))
  return (
    <div className="bars">
      {days.map((d) => (
        <div key={d.date} className="bar-col" title={`${d.date}: ${d.tickets}`}>
          <div className="bar-v" style={{ height: `${Math.max(4, (100 * d.tickets) / max)}%` }} />
          <div className="bar-x">{d.date.slice(5)}</div>
          <div className="bar-n">{d.tickets}</div>
        </div>
      ))}
    </div>
  )
}

const LISTEN_CLOSE = { 4401: 'Your session expired — sign in again.', 4404: 'That call has already ended.', 4429: 'Too many people are listening to this call.' }
const sinceTime = (epochSeconds) => epochSeconds ? new Date(epochSeconds * 1000).toLocaleTimeString('en-IN', { hour: '2-digit', minute: '2-digit', timeZone: 'Asia/Kolkata' }) : ''

// Live panel: which calls are on the line right now, a rolling transcript feed, and a
// Listen button per call (both sides, listen-only, about a second behind).
function LivePanel({ initial }) {
  const [calls, setCalls] = useState(initial)
  const [lines, setLines] = useState([])
  const [status, setStatus] = useState('connecting')
  const [listening, setListening] = useState(null)      // { call_sid, state: 'connecting'|'live'|'error', msg }
  const wsRef = useRef(null)
  const playerRef = useRef(null)                        // { ws, ctx, playAt, sid }

  useEffect(() => { setCalls(initial) }, [initial])

  function stopListening(msg) {
    const p = playerRef.current
    playerRef.current = null
    if (p) {
      try { p.ws.onclose = null; p.ws.close() } catch {}
      try { p.ctx.close() } catch {}
    }
    setListening(msg ? { state: 'error', msg } : null)
  }

  async function listen(sid) {
    stopListening()
    setListening({ call_sid: sid, state: 'connecting' })
    let ctx
    try { ctx = new AudioContext(); await ctx.resume() }
    catch { setListening({ call_sid: sid, state: 'error', msg: 'Audio playback is not available in this browser.' }); return }
    try {
      const { token } = await api.post('/live/token')
      const proto = location.protocol === 'https:' ? 'wss' : 'ws'
      const ws = new WebSocket(`${proto}://${location.host}/live/listen/${encodeURIComponent(sid)}?token=${encodeURIComponent(token)}`)
      ws.binaryType = 'arraybuffer'
      const p = { ws, ctx, playAt: 0, sid }
      playerRef.current = p
      ws.onopen = () => setListening({ call_sid: sid, state: 'live' })
      ws.onmessage = (e) => {
        if (typeof e.data === 'string') {
          try { if (JSON.parse(e.data).type === 'listen_end') stopListening() } catch {}
          return
        }
        const pcm = new Int16Array(e.data)
        if (!pcm.length) return
        // 8 kHz buffers; the audio node resamples to the device rate. A 250 ms jitter buffer
        // rides out network hiccups; a backlog over a second (the tab was asleep) is dropped.
        const buf = ctx.createBuffer(1, pcm.length, 8000)
        const ch = buf.getChannelData(0)
        for (let i = 0; i < pcm.length; i++) ch[i] = pcm[i] / 32768
        const now = ctx.currentTime
        if (p.playAt < now) p.playAt = now + 0.25
        if (p.playAt - now > 1.0) return
        const src = ctx.createBufferSource()
        src.buffer = buf; src.connect(ctx.destination); src.start(p.playAt)
        p.playAt += buf.duration
      }
      ws.onclose = (ev) => { if (playerRef.current === p) stopListening(LISTEN_CLOSE[ev.code] || null) }
      ws.onerror = () => {}
    } catch (e) { stopListening(e.message) }
  }

  useEffect(() => {
    let closed = false
    async function connect() {
      try {
        const { token } = await api.post('/live/token')
        const proto = location.protocol === 'https:' ? 'wss' : 'ws'
        const ws = new WebSocket(`${proto}://${location.host}/live/ws?token=${encodeURIComponent(token)}`)
        wsRef.current = ws
        ws.onopen = () => setStatus('live')
        ws.onclose = () => { setStatus('offline'); if (!closed) setTimeout(connect, 5000) }
        ws.onmessage = (e) => {
          const msg = JSON.parse(e.data)
          if (msg.type === 'active_calls') setCalls(msg.calls || [])
          else if (msg.type === 'call_start') { setCalls((c) => [...c.filter((x) => x.call_sid !== msg.call_sid), { call_sid: msg.call_sid, caller: msg.caller, caller_name: msg.caller_name, started_at: Date.now() / 1000 }]); push('system', `Call started · ${msg.caller_name ? `${msg.caller_name} · ` : ''}${msg.caller || 'unknown'}`) }
          else if (msg.type === 'call_end') {
            setCalls((c) => c.filter((x) => x.call_sid !== msg.call_sid))
            if (playerRef.current?.sid === msg.call_sid) stopListening()
            push('system', `Call ended${msg.ticket_id ? ` · ${msg.ticket_id}` : ''}`)
          }
          else if (msg.type === 'user' && msg.text) push('user', msg.text)
          else if (msg.type === 'gemini' && msg.text) push('agent', msg.text)
          else if (msg.type === 'tool_call') push('tool', `${msg.name} → ${msg.result?.ticket_id || msg.result?.status || (msg.result?.ok === false ? 'failed' : '')}`)
        }
      } catch { setStatus('offline'); if (!closed) setTimeout(connect, 5000) }
    }
    function push(who, text) {
      setLines((l) => {
        const last = l[l.length - 1]
        if (last && last.who === who && who !== 'system' && who !== 'tool') return [...l.slice(0, -1), { who, text: last.text + text }]
        return [...l.slice(-60), { who, text }]
      })
    }
    connect()
    return () => { closed = true; try { wsRef.current?.close() } catch {}; stopListening() }
  }, []) // eslint-disable-line react-hooks/exhaustive-deps

  return (
    <div className="panel">
      <div className="panel-head">
        <h3>Live calls {status === 'live' && <span className="live-dot" style={{ marginLeft: 8 }} />}</h3>
        <span style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
          {listening?.state === 'live' && <span className="pill red"><span className="dot" />Listening</span>}
          <span className="muted" style={{ fontSize: '0.78rem' }}>{status === 'live' ? `${calls.length} on the line` : status}</span>
        </span>
      </div>
      {!!calls.length && (
        <div className="stack" style={{ gap: 6, marginBottom: 10 }}>
          {calls.map((c) => {
            const on = listening?.call_sid === c.call_sid && listening.state !== 'error'
            return (
              <div key={c.call_sid} className="row-between" style={{ fontSize: '0.82rem' }}>
                <span className="pill blue"><span className="dot" />{c.caller_name ? `${c.caller_name} · ` : ''}{c.caller || 'unknown'}</span>
                <span className="muted" style={{ fontSize: '0.74rem' }}>{c.started_at ? `since ${sinceTime(c.started_at)}` : ''}</span>
                {on
                  ? <button className="btn danger sm" onClick={() => stopListening()}>{listening.state === 'connecting' ? 'Connecting…' : 'Stop'}</button>
                  : <button className="btn ghost sm" title="Hear both sides of this call (listen-only; the caller is not affected)" onClick={() => listen(c.call_sid)}>Listen</button>}
              </div>
            )
          })}
        </div>
      )}
      {listening?.state === 'error' && <div className="err" style={{ marginBottom: 8 }}>{listening.msg}</div>}
      <div style={{ maxHeight: 180, overflow: 'auto', fontSize: '0.8rem', lineHeight: 1.5 }}>
        {!lines.length ? <div className="muted">Transcripts of calls in progress appear here.</div> : lines.map((l, i) => (
          <div key={i}>
            <b style={{ color: l.who === 'user' ? 'var(--blue)' : l.who === 'agent' ? 'var(--green)' : l.who === 'tool' ? 'var(--amber)' : 'var(--muted)' }}>
              {l.who === 'user' ? 'Caller' : l.who === 'agent' ? 'Agent' : l.who === 'tool' ? 'Tool' : '·'}:
            </b>{' '}{l.text}
          </div>
        ))}
      </div>
    </div>
  )
}
