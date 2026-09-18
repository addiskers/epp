import { useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { api } from '../api.js'
import PageHeader from '../components/PageHeader.jsx'

export default function Agents() {
  const navigate = useNavigate()
  const [items, setItems] = useState([])
  const [err, setErr] = useState('')

  useEffect(() => { api.get('/agents').then((r) => setItems(r.items || [])).catch((e) => setErr(e.message)) }, [])

  return (
    <div className="stack">
      <PageHeader title="Agent" sub="What the helpline says on every call. Edit the script, then test it before going live." />
      {err && <div className="err">{err}</div>}
      <div style={{ display: 'grid', gap: 12, gridTemplateColumns: 'repeat(auto-fill,minmax(320px,1fr))' }}>
        {items.map((a) => (
          <div key={a.id} className="card" style={{ padding: 16 }}>
            <div style={{ display: 'flex', justifyContent: 'space-between', gap: 8 }}>
              <h4 style={{ margin: 0 }}>{a.name}</h4>
              <span className={`pill ${a.active ? 'green' : 'red'}`}>{a.active ? 'Live' : 'Inactive'}</span>
            </div>
            <p className="muted" style={{ fontSize: '0.82rem', margin: '8px 0 0', minHeight: 34 }}>{a.description || 'No description'}</p>
            <div className="muted" style={{ fontSize: '0.76rem', marginTop: 10 }}>
              Voice {a.voice_name || 'default'} · accent {a.speech_language_code || 'default'}
            </div>
            <div style={{ display: 'flex', gap: 8, marginTop: 14 }}>
              <button className="btn sm" onClick={() => navigate(`/agents/${a.id}`)}>Edit &amp; test</button>
            </div>
          </div>
        ))}
      </div>
    </div>
  )
}
