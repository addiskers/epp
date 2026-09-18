import { useCallback, useEffect, useState } from 'react'
import { api, CALLER_TYPES, cap } from '../api.js'
import Modal from '../components/Modal.jsx'
import PageHeader from '../components/PageHeader.jsx'

// Departments + the category -> department mapping the agent routes by. Every change is
// picked up by the next call (the server drops its cached prompt/tool context on save).
export default function Routing() {
  const [depts, setDepts] = useState([])
  const [cats, setCats] = useState([])
  const [err, setErr] = useState('')
  const [addDept, setAddDept] = useState(false)
  const [addCat, setAddCat] = useState(null)   // caller_type when open

  const load = useCallback(async () => {
    try {
      const [d, c] = await Promise.all([api.get('/departments'), api.get('/categories')])
      setDepts(d.items || []); setCats(c.items || []); setErr('')
    } catch (e) { setErr(e.message) }
  }, [])
  useEffect(() => { load() }, [load])

  async function patchDept(id, body) {
    try { await api.patch(`/departments/${id}`, body); load() } catch (e) { setErr(e.message) }
  }
  async function patchCat(id, body) {
    try { await api.patch(`/categories/${id}`, body); load() } catch (e) { setErr(e.message) }
  }
  async function delCat(c) {
    if (!confirm(`Delete "${c.name}" for ${c.caller_type}s? Existing tickets keep their category.`)) return
    try { await api.del(`/categories/${c.id}`); load() } catch (e) { setErr(e.message) }
  }

  return (
    <div className="stack">
      <PageHeader title="Routing" sub="Which department owns which kind of concern. Changes apply to the very next call." />
      {err && <div className="err">{err}</div>}

      <div className="panel">
        <div className="panel-head">
          <h3>Departments</h3>
          <button className="btn sm" onClick={() => setAddDept(true)}>+ Add department</button>
        </div>
        <div className="table-wrap">
          <table>
            <thead><tr><th className="no-sort">Name</th><th className="no-sort">Code</th><th className="no-sort">Categories</th><th className="no-sort">Status</th><th className="no-sort"></th></tr></thead>
            <tbody>
              {depts.map((d) => (
                <tr key={d.id}>
                  <td><Inline value={d.name} onSave={(v) => patchDept(d.id, { name: v })} /></td>
                  <td style={{ fontFamily: 'var(--mono)' }}>{d.code}</td>
                  <td className="muted">{cats.filter((c) => c.department_id === d.id).length}</td>
                  <td><span className={`pill ${d.active ? 'valid' : 'invalid'}`}>{d.active ? 'Active' : 'Inactive'}</span></td>
                  <td><button className={`btn sm ${d.active ? 'ghost' : ''}`} onClick={() => patchDept(d.id, { active: !d.active })}>{d.active ? 'Deactivate' : 'Activate'}</button></td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>

      {CALLER_TYPES.map((ct) => (
        <div className="panel" key={ct}>
          <div className="panel-head">
            <div><h3>{cap(ct)} categories</h3>
              <div className="muted" style={{ fontSize: '0.78rem', marginTop: 3 }}>The agent picks one of these on every {ct} call. "Other" is the fallback and cannot be removed.</div></div>
            <button className="btn sm" onClick={() => setAddCat(ct)}>+ Add category</button>
          </div>
          <div className="table-wrap">
            <table>
              <thead><tr>
                <th className="no-sort">Category</th><th className="no-sort">Routes to</th>
                <th className="no-sort" title="Every ticket in this category is high priority and flagged for escalation">High priority</th>
                <th className="no-sort" title="Used by the post-call classifier when the live agent recorded nothing">Keywords</th>
                <th className="no-sort">Active</th><th className="no-sort"></th>
              </tr></thead>
              <tbody>
                {cats.filter((c) => c.caller_type === ct).map((c) => (
                  <tr key={c.id} style={{ opacity: c.active ? 1 : 0.55 }}>
                    <td style={{ fontWeight: 600 }}>{c.name}</td>
                    <td>
                      <select value={c.department_id || ''} onChange={(e) => patchCat(c.id, { department_id: e.target.value ? Number(e.target.value) : '' })} style={{ minWidth: 170 }}>
                        <option value="">— unassigned —</option>
                        {depts.filter((d) => d.active || d.id === c.department_id).map((d) => <option key={d.id} value={d.id}>{d.name}</option>)}
                      </select>
                    </td>
                    <td><input type="checkbox" checked={!!c.high_priority} onChange={(e) => patchCat(c.id, { high_priority: e.target.checked })} /></td>
                    <td><Inline value={(safeList(c.keywords)).join(', ')} placeholder="comma-separated" onSave={(v) => patchCat(c.id, { keywords: v })} width={260} /></td>
                    <td><input type="checkbox" checked={!!c.active} disabled={c.name === 'Other'} onChange={(e) => patchCat(c.id, { active: e.target.checked })} /></td>
                    <td>{c.name !== 'Other' && <button className="btn ghost sm" onClick={() => delCat(c)}>Delete</button>}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      ))}

      {addDept && <DeptModal onClose={() => setAddDept(false)} onDone={() => { setAddDept(false); load() }} />}
      {addCat && <CatModal callerType={addCat} depts={depts.filter((d) => d.active)} onClose={() => setAddCat(null)} onDone={() => { setAddCat(null); load() }} />}
    </div>
  )
}

function safeList(v) {
  if (Array.isArray(v)) return v
  try { const p = JSON.parse(v || '[]'); return Array.isArray(p) ? p : [] } catch { return [] }
}

function Inline({ value, onSave, placeholder, width = 200 }) {
  const [v, setV] = useState(value || '')
  useEffect(() => { setV(value || '') }, [value])
  return (
    <input value={v} placeholder={placeholder} style={{ width, height: 30, fontSize: '0.8rem' }}
           onChange={(e) => setV(e.target.value)}
           onBlur={() => { if (v.trim() !== (value || '')) onSave(v.trim()) }}
           onKeyDown={(e) => { if (e.key === 'Enter') e.target.blur() }} />
  )
}

function DeptModal({ onClose, onDone }) {
  const [f, setF] = useState({ name: '', code: '' })
  const [err, setErr] = useState('')
  const [busy, setBusy] = useState(false)
  async function save() {
    setBusy(true); setErr('')
    try { await api.post('/departments', f); onDone() } catch (e) { setErr(e.message) } finally { setBusy(false) }
  }
  return (
    <Modal title="Add department" onClose={() => !busy && onClose()}
           footer={<><button className="btn ghost" onClick={onClose}>Cancel</button><button className="btn" disabled={busy || !f.name || !f.code} onClick={save}>Create</button></>}>
      {err && <div className="err" style={{ marginBottom: 10 }}>{err}</div>}
      <div className="row"><label>Name</label><input value={f.name} onChange={(e) => setF({ ...f, name: e.target.value })} autoFocus /></div>
      <div className="row"><label>Short code</label><input value={f.code} onChange={(e) => setF({ ...f, code: e.target.value.toUpperCase() })} placeholder="e.g. LEGAL" /></div>
    </Modal>
  )
}

function CatModal({ callerType, depts, onClose, onDone }) {
  const [f, setF] = useState({ caller_type: callerType, name: '', department_id: '', keywords: '', high_priority: false })
  const [err, setErr] = useState('')
  const [busy, setBusy] = useState(false)
  async function save() {
    setBusy(true); setErr('')
    try {
      await api.post('/categories', { ...f, department_id: f.department_id ? Number(f.department_id) : null })
      onDone()
    } catch (e) { setErr(e.message) } finally { setBusy(false) }
  }
  return (
    <Modal title={`Add ${callerType} category`} onClose={() => !busy && onClose()}
           footer={<><button className="btn ghost" onClick={onClose}>Cancel</button><button className="btn" disabled={busy || !f.name} onClick={save}>Create</button></>}>
      {err && <div className="err" style={{ marginBottom: 10 }}>{err}</div>}
      <div className="row"><label>Category name</label><input value={f.name} onChange={(e) => setF({ ...f, name: e.target.value })} autoFocus /></div>
      <div className="row"><label>Routes to</label>
        <select value={f.department_id} onChange={(e) => setF({ ...f, department_id: e.target.value })}>
          <option value="">— unassigned —</option>
          {depts.map((d) => <option key={d.id} value={d.id}>{d.name}</option>)}
        </select>
      </div>
      <div className="row"><label>Keywords (comma-separated)</label><input value={f.keywords} onChange={(e) => setF({ ...f, keywords: e.target.value })} placeholder="used by the post-call classifier" /></div>
      <label className="toggle"><input type="checkbox" checked={f.high_priority} onChange={(e) => setF({ ...f, high_priority: e.target.checked })} /> Always high priority</label>
    </Modal>
  )
}
