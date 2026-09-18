import { useState } from 'react'
import { Link } from 'react-router-dom'
import { api, CALLER_TYPES, cap } from '../api.js'
import ContactUpload from '../components/ContactUpload.jsx'
import ContactsTable from '../components/ContactsTable.jsx'
import Modal from '../components/Modal.jsx'
import PageHeader from '../components/PageHeader.jsx'

const BLANK = { name: '', phone: '', caller_type: '', notes: '' }

export default function Contacts() {
  const [refreshKey, setRefreshKey] = useState(0)
  const [selected, setSelected] = useState(new Set())
  const [show, setShow] = useState(false)
  const [f, setF] = useState(BLANK)
  const [err, setErr] = useState('')
  const [busy, setBusy] = useState(false)

  const refresh = () => setRefreshKey((k) => k + 1)
  function toggle(id) { setSelected((s) => { const n = new Set(s); n.has(id) ? n.delete(id) : n.add(id); return n }) }
  function toggleMany(ids, on) { setSelected((s) => { const n = new Set(s); ids.forEach((id) => on ? n.add(id) : n.delete(id)); return n }) }

  async function add() {
    setBusy(true); setErr('')
    try { await api.post('/contacts', f); setShow(false); setF(BLANK); refresh() }
    catch (e) { setErr(e.message) } finally { setBusy(false) }
  }

  async function deleteSelected() {
    if (!selected.size || !confirm(`Delete ${selected.size} contact(s)? Campaigns already created keep their recipients.`)) return
    await api.post('/contacts/delete', { ids: [...selected] })
    setSelected(new Set()); refresh()
  }

  return (
    <div className="stack">
      <PageHeader title="Contacts" sub="The numbers a campaign can call. Upload a sheet or add them one at a time."
                  actions={<>
                    {selected.size > 0 && <button className="btn danger sm" onClick={deleteSelected}>Delete ({selected.size})</button>}
                    <button className="btn ghost sm" onClick={() => { setErr(''); setShow(true) }}>+ Add contact</button>
                    <Link to="/campaigns/new" className="btn sm">Create campaign</Link>
                  </>} />
      <ContactUpload onImported={refresh} />
      <div className="panel">
        <div className="panel-head"><h3>All contacts</h3></div>
        <ContactsTable selectable selected={selected} onToggle={toggle} onToggleMany={toggleMany} refreshKey={refreshKey} />
      </div>
      {show && (
        <Modal title="Add contact" onClose={() => !busy && setShow(false)}
               footer={<><button className="btn ghost" disabled={busy} onClick={() => setShow(false)}>Cancel</button>
                         <button className="btn" disabled={busy || !f.phone} onClick={add}>{busy ? 'Adding…' : 'Add'}</button></>}>
          {err && <div className="err" style={{ marginBottom: 10 }}>{err}</div>}
          <div className="row"><label>Name</label><input value={f.name} onChange={(e) => setF({ ...f, name: e.target.value })} autoFocus /></div>
          <div className="row"><label>Phone</label><input value={f.phone} onChange={(e) => setF({ ...f, phone: e.target.value })} placeholder="9876543210 or +9198…" /></div>
          <div className="row"><label>Type</label>
            <select value={f.caller_type} onChange={(e) => setF({ ...f, caller_type: e.target.value })}>
              <option value="">Not sure</option>
              {CALLER_TYPES.map((t) => <option key={t} value={t}>{cap(t)}</option>)}
            </select></div>
          <div className="row"><label>Notes</label><input value={f.notes} onChange={(e) => setF({ ...f, notes: e.target.value })} placeholder="plant, company, anything useful" /></div>
        </Modal>
      )}
    </div>
  )
}
