import { useEffect, useState } from 'react'
import { api, fmtDate } from '../api.js'
import { useAuth } from '../auth.jsx'
import Modal from '../components/Modal.jsx'
import PageHeader from '../components/PageHeader.jsx'

const BLANK = { username: '', name: '', password: '', role: 'dept_user', department_id: '' }

export default function Users() {
  const { user: me } = useAuth()
  const [items, setItems] = useState([])
  const [depts, setDepts] = useState([])
  const [err, setErr] = useState('')
  const [show, setShow] = useState(false)
  const [f, setF] = useState(BLANK)
  const [formErr, setFormErr] = useState('')
  const [busy, setBusy] = useState(false)

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

  async function patch(u, body) {
    try { await api.patch(`/users/${u.id}`, body); load() } catch (e) { alert(e.message) }
  }

  function resetPassword(u) {
    const p = prompt(`New password for ${u.username} (min 8 characters):`)
    if (p) patch(u, { password: p })
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
            <th className="no-sort">Department</th><th className="no-sort">Status</th><th className="no-sort">Created</th><th className="no-sort">Actions</th>
          </tr></thead>
          <tbody>
            {items.length === 0 ? <tr><td colSpan={7} className="empty">No users.</td></tr> : items.map((u) => (
              <tr key={u.id}>
                <td style={{ fontWeight: 600 }}>{u.username}{u.id === me?.id && <span className="muted"> (you)</span>}</td>
                <td>{u.name || <span className="muted">—</span>}</td>
                <td>
                  <select value={u.role} disabled={u.id === me?.id} onChange={(e) => patch(u, { role: e.target.value })}>
                    <option value="admin">Admin</option><option value="dept_user">Department user</option>
                  </select>
                </td>
                <td>
                  <select value={u.department_id || ''} onChange={(e) => patch(u, { department_id: e.target.value ? Number(e.target.value) : null })}>
                    <option value="">—</option>
                    {depts.map((d) => <option key={d.id} value={d.id}>{d.name}</option>)}
                  </select>
                </td>
                <td><span className={`pill ${u.active ? 'valid' : 'invalid'}`}>{u.active ? 'Active' : 'Disabled'}</span></td>
                <td>{fmtDate(u.created_at)}</td>
                <td style={{ display: 'flex', gap: 6 }}>
                  <button className="btn ghost sm" onClick={() => resetPassword(u)}>Reset password</button>
                  {u.id !== me?.id && <button className={`btn sm ${u.active ? 'danger' : ''}`} onClick={() => patch(u, { active: !u.active })}>{u.active ? 'Disable' : 'Enable'}</button>}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

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
