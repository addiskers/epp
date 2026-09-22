import { useCallback, useEffect, useState } from 'react'
import { api, CALLER_TYPES, cap } from '../api.js'
import Modal from '../components/Modal.jsx'
import PageHeader from '../components/PageHeader.jsx'

// Departments + the category -> department mapping the agent routes by. Every control edits a
// draft; a panel's "Save changes" sends the changed rows to the server, then shows "Saved" or
// names the row that failed. The next call picks the new mapping up.
const DEPT_KEYS = ['name', 'code', 'active']
const CAT_KEYS = ['department_id', 'high_priority', 'keywords', 'active']

function safeList(v) {
  if (Array.isArray(v)) return v
  try { const p = JSON.parse(v || '[]'); return Array.isArray(p) ? p : [] } catch { return [] }
}
const deptBase = (d) => ({ name: d.name || '', code: d.code || '', active: !!d.active })
const catBase = (c) => ({ department_id: c.department_id ? String(c.department_id) : '', high_priority: !!c.high_priority,
                          keywords: safeList(c.keywords).join(', '), active: !!c.active })
function diff(base, draft, keys) {
  const out = {}
  for (const k of keys) if (draft && k in draft && String(draft[k]) !== String(base[k])) out[k] = draft[k]
  return out
}

export default function Routing() {
  const [depts, setDepts] = useState([])
  const [cats, setCats] = useState([])
  const [err, setErr] = useState('')
  const [addDept, setAddDept] = useState(false)
  const [addCat, setAddCat] = useState(null)   // caller_type when open
  const [dDraft, setDDraft] = useState({})     // dept id -> partial
  const [cDraft, setCDraft] = useState({})     // category id -> partial
  const [panelErr, setPanelErr] = useState({})
  const [savedPanel, setSavedPanel] = useState({})
  const [busy, setBusy] = useState('')

  const load = useCallback(async () => {
    try {
      const [d, c] = await Promise.all([api.get('/departments'), api.get('/categories')])
      setDepts(d.items || []); setCats(c.items || []); setErr('')
      setDDraft({}); setCDraft({})
    } catch (e) { setErr(e.message) }
  }, [])
  useEffect(() => { load() }, [load])

  const editDept = (id, patch) => setDDraft((s) => ({ ...s, [id]: { ...(s[id] || {}), ...patch } }))
  const editCat = (id, patch) => setCDraft((s) => ({ ...s, [id]: { ...(s[id] || {}), ...patch } }))
  const deptView = (d) => ({ ...deptBase(d), ...(dDraft[d.id] || {}) })
  const catView = (c) => ({ ...catBase(c), ...(cDraft[c.id] || {}) })
  const deptChanges = depts.map((d) => [d, diff(deptBase(d), dDraft[d.id], DEPT_KEYS)]).filter(([, ch]) => Object.keys(ch).length)
  const catChanges = (ct) => cats.filter((c) => c.caller_type === ct)
    .map((c) => [c, diff(catBase(c), cDraft[c.id], CAT_KEYS)]).filter(([, ch]) => Object.keys(ch).length)

  async function savePanel(key, rows, path) {
    setBusy(key); setPanelErr((p) => ({ ...p, [key]: '' }))
    try {
      for (const [row, ch] of rows) {
        const body = { ...ch }
        if ('department_id' in body) body.department_id = body.department_id ? Number(body.department_id) : ''
        try { await api.patch(`${path}/${row.id}`, body) }
        catch (e) { throw new Error(`${row.name}: ${e.message}`) }
      }
      await load()
      setSavedPanel((s) => ({ ...s, [key]: true }))
      setTimeout(() => setSavedPanel((s) => ({ ...s, [key]: false })), 2500)
    } catch (e) { setPanelErr((p) => ({ ...p, [key]: e.message })) } finally { setBusy('') }
  }
  function discardPanel(key, ids, which) {
    const drop = (s) => { const n = { ...s }; ids.forEach((id) => delete n[id]); return n }
    if (which === 'dept') setDDraft(drop); else setCDraft(drop)
    setPanelErr((p) => ({ ...p, [key]: '' }))
  }
  async function delCat(c) {
    if (!confirm(`Delete "${c.name}" for ${c.caller_type}s? Existing tickets keep their category.`)) return
    try { await api.del(`/categories/${c.id}`); load() } catch (e) { setErr(e.message) }
  }

  return (
    <div className="stack">
      <PageHeader title="Departments & Routing" sub="The departments, and which one owns which kind of concern. Saved changes apply to the very next call." />
      {err && <div className="err">{err}</div>}

      <div className="panel">
        <div className="panel-head">
          <h3>Departments</h3>
          <button className="btn sm" onClick={() => setAddDept(true)}>+ Add department</button>
        </div>
        <div className="table-wrap">
          <table>
            <thead><tr><th className="no-sort">Name</th><th className="no-sort">Code</th><th className="no-sort">Categories</th><th className="no-sort">Active</th></tr></thead>
            <tbody>
              {depts.map((d) => {
                const v = deptView(d)
                const changed = dDraft[d.id] && Object.keys(diff(deptBase(d), dDraft[d.id], DEPT_KEYS)).length > 0
                return (
                  <tr key={d.id} style={{ opacity: v.active ? 1 : 0.6, background: changed ? 'rgba(16,185,129,0.05)' : undefined }}>
                    <td><input value={v.name} style={{ width: 220, height: 30, fontSize: '0.8rem' }} onChange={(e) => editDept(d.id, { name: e.target.value })} /></td>
                    <td><input value={v.code} style={{ width: 110, height: 30, fontSize: '0.8rem', fontFamily: 'var(--mono)' }} maxLength={16}
                               onChange={(e) => editDept(d.id, { code: e.target.value.toUpperCase() })} /></td>
                    <td className="muted">{cats.filter((c) => c.department_id === d.id).length}</td>
                    <td><input type="checkbox" checked={v.active} onChange={(e) => editDept(d.id, { active: e.target.checked })} /></td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        </div>
        <PanelFooter n={deptChanges.length} busy={busy === 'depts'} saved={savedPanel.depts} err={panelErr.depts}
                     onSave={() => savePanel('depts', deptChanges, '/departments')}
                     onDiscard={() => discardPanel('depts', deptChanges.map(([d]) => d.id), 'dept')} />
      </div>

      {CALLER_TYPES.map((ct) => {
        const key = `cat:${ct}`
        const changes = catChanges(ct)
        return (
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
                  {cats.filter((c) => c.caller_type === ct).map((c) => {
                    const v = catView(c)
                    const changed = cDraft[c.id] && Object.keys(diff(catBase(c), cDraft[c.id], CAT_KEYS)).length > 0
                    return (
                      <tr key={c.id} style={{ opacity: v.active ? 1 : 0.55, background: changed ? 'rgba(16,185,129,0.05)' : undefined }}>
                        <td style={{ fontWeight: 600 }}>{c.name}</td>
                        <td>
                          <select value={v.department_id} onChange={(e) => editCat(c.id, { department_id: e.target.value })} style={{ minWidth: 170 }}>
                            <option value="">— unassigned —</option>
                            {depts.filter((d) => d.active || d.id === c.department_id).map((d) => <option key={d.id} value={String(d.id)}>{d.name}</option>)}
                          </select>
                        </td>
                        <td><input type="checkbox" checked={v.high_priority} onChange={(e) => editCat(c.id, { high_priority: e.target.checked })} /></td>
                        <td><input value={v.keywords} placeholder="comma-separated" style={{ width: 260, height: 30, fontSize: '0.8rem' }}
                                   onChange={(e) => editCat(c.id, { keywords: e.target.value })} /></td>
                        <td><input type="checkbox" checked={v.active} disabled={c.name === 'Other'} onChange={(e) => editCat(c.id, { active: e.target.checked })} /></td>
                        <td>{c.name !== 'Other' && <button className="btn ghost sm" onClick={() => delCat(c)}>Delete</button>}</td>
                      </tr>
                    )
                  })}
                </tbody>
              </table>
            </div>
            <PanelFooter n={changes.length} busy={busy === key} saved={savedPanel[key]} err={panelErr[key]}
                         onSave={() => savePanel(key, changes, '/categories')}
                         onDiscard={() => discardPanel(key, changes.map(([c]) => c.id), 'cat')} />
          </div>
        )
      })}

      {addDept && <DeptModal onClose={() => setAddDept(false)} onDone={() => { setAddDept(false); load() }} />}
      {addCat && <CatModal callerType={addCat} depts={depts.filter((d) => d.active)} onClose={() => setAddCat(null)} onDone={() => { setAddCat(null); load() }} />}
    </div>
  )
}

function PanelFooter({ n, busy, saved, err, onSave, onDiscard }) {
  return (
    <div style={{ display: 'flex', gap: 8, alignItems: 'center', flexWrap: 'wrap', marginTop: 12 }}>
      <button className="btn" disabled={busy || !n} onClick={onSave}>{busy ? 'Saving…' : n ? `Save changes (${n})` : 'Save changes'}</button>
      {!!n && !busy && <button className="btn ghost sm" onClick={onDiscard}>Discard</button>}
      {saved && <span className="pill green">Saved ✓</span>}
      {!n && !saved && <span className="muted" style={{ fontSize: '0.74rem' }}>Edit a row above, then save.</span>}
      {err && <span style={{ color: 'var(--red)', fontSize: '0.8rem' }}>Not saved — {err}</span>}
    </div>
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
