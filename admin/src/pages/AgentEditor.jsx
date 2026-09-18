import { useCallback, useEffect, useRef, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { api } from '../api.js'
import AgentTestPanel from '../components/AgentTestPanel.jsx'
import PageHeader from '../components/PageHeader.jsx'
import PlaceholderPalette from '../components/PlaceholderPalette.jsx'

const VOICES = ['', 'Aoede', 'Kore', 'Leda', 'Zephyr', 'Puck', 'Charon', 'Fenrir']

export default function AgentEditor() {
  const { id } = useParams()
  const [agent, setAgent] = useState(null)
  const [known, setKnown] = useState([])
  const [f, setF] = useState({ name: '', prompt_template: '', trigger_template: '', voice_name: '', speech_language_code: '' })
  const [err, setErr] = useState('')
  const [saved, setSaved] = useState(false)
  const [busy, setBusy] = useState(false)
  const promptRef = useRef(null)

  const dirty = agent && ['name', 'prompt_template', 'trigger_template', 'voice_name', 'speech_language_code']
    .some((k) => (f[k] || '') !== (agent[k] || ''))

  const load = useCallback(async () => {
    try {
      const [a, list] = await Promise.all([api.get(`/agents/${id}`), api.get('/agents')])
      setAgent(a); setKnown(list.placeholders || [])
      setF({ name: a.name || '', prompt_template: a.prompt_template || '', trigger_template: a.trigger_template || '',
             voice_name: a.voice_name || '', speech_language_code: a.speech_language_code || '' })
    } catch (e) { setErr(e.message) }
  }, [id])
  useEffect(() => { load() }, [load])

  function insert(text) {
    const el = promptRef.current
    if (!el) { setF((p) => ({ ...p, prompt_template: p.prompt_template + text })); return }
    const { selectionStart: s, selectionEnd: e } = el
    setF((p) => ({ ...p, prompt_template: p.prompt_template.slice(0, s) + text + p.prompt_template.slice(e) }))
    requestAnimationFrame(() => { el.focus(); el.setSelectionRange(s + text.length, s + text.length) })
  }

  async function save() {
    setErr(''); setBusy(true); setSaved(false)
    try {
      const updated = await api.patch(`/agents/${agent.id}`, f)
      setAgent(updated); setSaved(true)
      setTimeout(() => setSaved(false), 2500)
    } catch (e) { setErr(e.message) } finally { setBusy(false) }
  }

  async function reset() {
    if (!confirm('Replace the script with the shipped version? Your edits will be lost.')) return
    setBusy(true); setErr('')
    try { await api.post(`/agents/${agent.id}/reset`); await load() } catch (e) { setErr(e.message) } finally { setBusy(false) }
  }

  if (err && !agent) return <div className="stack"><div className="err">{err}</div></div>
  if (!agent) return <div className="panel"><div className="muted">Loading…</div></div>

  return (
    <div className="stack">
      <PageHeader title={agent.name} sub={agent.description}
        back={<Link to="/agents" className="muted" style={{ fontSize: '0.82rem' }}>← Agent</Link>}
        actions={<>
          {saved && <span className="pill green" style={{ marginRight: 8 }}>Saved</span>}
          <button className="btn ghost" disabled={busy} onClick={reset}>Reset to shipped script</button>
          <button className="btn" disabled={busy || !dirty} onClick={save}>{busy ? 'Saving…' : 'Save changes'}</button>
        </>} />
      {err && <div className="err">{err}</div>}

      <div style={{ display: 'grid', gap: 16, gridTemplateColumns: 'minmax(0,1fr) 280px', alignItems: 'start' }}>
        <div className="panel">
          <div className="panel-head"><h3>What it says</h3></div>
          <div className="two" style={{ display: 'grid', gridTemplateColumns: '1fr 1fr 1fr', gap: 12 }}>
            <div><label>Agent name</label><input value={f.name} onChange={(e) => setF({ ...f, name: e.target.value })} /></div>
            <div><label>Voice</label>
              <select value={f.voice_name} onChange={(e) => setF({ ...f, voice_name: e.target.value })}>
                {VOICES.map((v) => <option key={v} value={v}>{v || 'Server default'}</option>)}
              </select></div>
            <div><label>Accent (BCP-47)</label><input value={f.speech_language_code} placeholder="en-IN" onChange={(e) => setF({ ...f, speech_language_code: e.target.value })} /></div>
          </div>
          <div className="muted" style={{ fontSize: '0.74rem', marginTop: 4 }}>
            The accent is fixed for the whole call; the agent still switches languages by following the script's LANGUAGE section.
          </div>
          <div className="row" style={{ marginTop: 14 }}>
            <label>The script</label>
            <textarea ref={promptRef} rows={30} value={f.prompt_template} onChange={(e) => setF({ ...f, prompt_template: e.target.value })}
                      style={{ fontFamily: 'var(--mono)', fontSize: '0.79rem', lineHeight: 1.6, width: '100%' }} />
          </div>
          <div className="row">
            <label>Opening trigger</label>
            <textarea rows={3} value={f.trigger_template} onChange={(e) => setF({ ...f, trigger_template: e.target.value })}
                      style={{ fontFamily: 'var(--mono)', fontSize: '0.79rem', width: '100%' }} />
            <div className="muted" style={{ fontSize: '0.76rem', marginTop: 3 }}>What makes the agent speak first when a call connects.</div>
          </div>
        </div>
        <div className="panel" style={{ position: 'sticky', top: 16 }}>
          <div className="panel-head"><h3>Placeholders</h3></div>
          <PlaceholderPalette known={known} onInsert={insert} />
        </div>
      </div>

      <AgentTestPanel agent={agent} />
    </div>
  )
}
