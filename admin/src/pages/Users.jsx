import { useEffect, useState } from 'react'
import { api, fmtDate } from '../api.js'
import { useAuth } from '../auth.jsx'
import Modal from '../components/Modal.jsx'
import PageHeader from '../components/PageHeader.jsx'

const BLANK = { username: '', name: '', password: '', role: 'dept_user', department_id: '' }
const ROLE_LABEL = { admin: 'Admin', dept_user: 'Department user' }

export default function Users() {
  const { user: me } = useAuth()
  const [items, setItems] = useState([])
  const [depts, setDepts] = useState([])
  const [err, setErr] = useState('')
  const [show, setShow] = useState(false)
  const [editing, setEditing] = useState(null)
  const [f, setF] = useState(BLANK)
  const [formErr, setFormErr] = useState('')
  const [busy, setBusy] = useState(false)
  const [savedId, setSavedId] = useState(null)

  function load() {
    api.get('/users').then((d) => setItems(d.items || [])).catch((e) => setErr(e.message))
    api.get('/departments?active=1').then((d) => setDepts(d.items || [])).catch(() => {})
  }
  useEffect(() => { load() }, [])

  async function create() {
    setBusy(true); setFormErr('')
    try {
      await api.post('/users', { ...f, department_id: f.department_id ? Number(f.department_id) : null })
      setShow(false); setF(BLANK); load()
    } catch (e) { setFormErr(e.message) } finally { setBusy(false) }
  }

  function onSaved(u) {
    setEditing(null); load()
    setSavedId(u.id); setTimeout(() => setSavedId(null), 2500)
  }

  return (
    <div className="stack">
      <PageHeader title="Users" sub="Admins see everything. Department users see only their department's tickets."
                  actions={<button className="btn" onClick={() => { setFormErr(''); setShow(true) }}>+ Add user</button>} />
      {err && <div className="err">{err}</div>}

      <div className="table-wrap">
        <table>
          <thead><tr>
            <th className="no-sort">Username</th><th className="no-sort">Name</th><th className="no-sort">Role</th>
            <th className="no-sort">Department</th><th className="no-sort">Status</th><th className="no-sort">Created</th><th className="no-sort"></th>
          </tr></thead>
          <tbody>
            {items.length === 0 ? <tr><td colSpan={7} className="empty">No users.</td></tr> : items.map((u) => (
              <tr key={u.id}>
                <td style={{ fontWeight: 600 }}>{u.username}{u.id === me?.id && <span className="muted"> (you)</span>}</td>
                <td>{u.name || <span className="muted">—</span>}</td>
                <td>{ROLE_LABEL[u.role] || u.role}</td>
                <td>{u.department_name || <span className="muted">—</span>}</td>
                <td><span className={`pill ${u.active ? 'valid' : 'invalid'}`}>{u.active ? 'Active' : 'Disabled'}</span></td>
                <td>{fmtDate(u.created_at)}</td>
                <td style={{ display: 'flex', gap: 6, alignItems: 'center' }}>
                  <button className="btn ghost sm" onClick={() => setEditing(u)}>Edit</button>
                  {savedId === u.id && <span className="pill green">Saved ✓</span>}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {editing && <EditUserModal u={editing} depts={depts} me={me} onClose={() => setEditing(null)} onSaved={onSaved} />}

      {show && (
        <Modal title="Add user" sub="Create an admin or a department user" onClose={() => !busy && setShow(false)}
          footer={<>
            <button className="btn ghost" disabled={busy} onClick={() => setShow(false)}>Cancel</button>
            <button className="btn" disabled={busy || !f.username || !f.password || (f.role === 'dept_user' && !f.department_id)} onClick={create}>{busy ? 'Creating…' : 'Create user'}</button>
          </>}>
          {formErr && <div className="err" style={{ marginBottom: 12 }}>{formErr}</div>}
          <div className="row"><label>Username</label><input value={f.username} onChange={(e) => setF({ ...f, username: e.target.value })} autoFocus /></div>
          <div className="row"><label>Name</label><input value={f.name} onChange={(e) => setF({ ...f, name: e.target.value })} /></div>
          <div className="row"><label>Password</label><input type="password" value={f.password} onChange={(e) => setF({ ...f, password: e.target.value })} placeholder="min 8 characters" /></div>
          <div className="row"><label>Role</label>
            <select value={f.role} onChange={(e) => setF({ ...f, role: e.target.value })}>
              <option value="dept_user">Department user — their department's tickets only</option>
              <option value="admin">Admin — everything, including routing, agent, users and audit</option>
            </select>
          </div>
          <div className="row"><label>Department {f.role === 'dept_user' && <span style={{ color: 'var(--red)' }}>*</span>}</label>
            <select value={f.department_id} onChange={(e) => setF({ ...f, department_id: e.target.value })}>
              <option value="">—</option>
              {depts.map((d) => <option key={d.id} value={d.id}>{d.name}</option>)}
            </select>
          </div>
        </Modal>
      )}
    </div>
  )
}

// Edit one user. Fields are a draft; Save sends only what changed, in one request, and the
// server checks everything before it writes anything.
function EditUserModal({ u, depts, me, onClose, onSaved }) {
  const base = { name: u.name || '', role: u.role, department_id: u.department_id ? String(u.department_id) : '', active: !!u.active }
  const [f, setF] = useState({ ...base, password: '' })
  const [err, setErr] = useState('')
  const [busy, setBusy] = useState(false)
  const self = u.id === me?.id
  const changed = Object.keys(base).filter((k) => String(f[k]) !== String(base[k]))
  const dirty = changed.length > 0 || f.password.length > 0
  const needsDept = f.role === 'dept_user' && !f.department_id

  async function save() {
    const body = {}
    for (const k of changed) body[k] = k === 'department_id' ? (f.department_id ? Number(f.department_id) : null) : f[k]
    if (f.password) body.password = f.password
    setBusy(true); setErr('')
    try { const r = await api.patch(`/users/${u.id}`, body); onSaved(r.user || u) }
    catch (e) { setErr(e.message) } finally { setBusy(false) }
  }

  return (
    <Modal title={`Edit ${u.username}`} sub="Nothing changes until you press Save" onClose={() => !busy && onClose()}
      footer={<>
        <button className="btn ghost" disabled={busy} onClick={onClose}>Cancel</button>
        <button className="btn" disabled={busy || !dirty || needsDept} onClick={save}>{busy ? 'Saving…' : 'Save'}</button>
      </>}>
      {err && <div className="err" style={{ marginBottom: 12 }}>{err}</div>}
      <div className="row"><label>Username</label><input value={u.username} disabled /></div>
      <div className="row"><label>Name</label><input value={f.name} onChange={(e) => setF({ ...f, name: e.target.value })} autoFocus /></div>
      <div className="row"><label>Role</label>
        <select value={f.role} disabled={self} onChange={(e) => setF({ ...f, role: e.target.value })}>
          <option value="dept_user">Department user — their department's tickets only</option>
          <option value="admin">Admin — everything</option>
        </select>
        {self && <div className="muted" style={{ fontSize: '0.72rem', marginTop: 4 }}>You cannot change your own role.</div>}
      </div>
      <div className="row"><label>Department {f.role === 'dept_user' && <span style={{ color: 'var(--red)' }}>*</span>}</label>
        <select value={f.department_id} onChange={(e) => setF({ ...f, department_id: e.target.value })}>
          <option value="">—</option>
          {depts.map((d) => <option key={d.id} value={String(d.id)}>{d.name}</option>)}
        </select>
        {needsDept && <div className="muted" style={{ fontSize: '0.72rem', marginTop: 4 }}>A department user needs a department.</div>}
      </div>
      <div className="row"><label>New password</label>
        <input type="password" value={f.password} placeholder="leave blank to keep the current one" onChange={(e) => setF({ ...f, password: e.target.value })} />
        {f.password && f.password.length < 8 && <div className="muted" style={{ fontSize: '0.72rem', marginTop: 4 }}>At least 8 characters.</div>}
      </div>
      {!self && <label className="toggle"><input type="checkbox" checked={f.active} onChange={(e) => setF({ ...f, active: e.target.checked })} /> Account active (a disabled user cannot sign in)</label>}
    </Modal>
  )
}
