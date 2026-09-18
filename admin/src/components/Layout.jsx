import { NavLink, Outlet, useNavigate } from 'react-router-dom'
import { useAuth } from '../auth.jsx'
import {
  IconDashboard, IconTicket, IconRouting, IconAgent, IconLogs, IconUsers, IconAudit, IconUser, IconLogout,
  IconContacts, IconCampaigns, IconClock,
} from './icons.jsx'

const ADMIN_NAV = [
  { to: '/', label: 'Dashboard', icon: IconDashboard, end: true },
  { to: '/tickets', label: 'Tickets', icon: IconTicket },
  { to: '/campaigns', label: 'Campaigns', icon: IconCampaigns },
  { to: '/contacts', label: 'Contacts', icon: IconContacts },
  { to: '/scheduler', label: 'Scheduler', icon: IconClock },
  { to: '/routing', label: 'Routing', icon: IconRouting },
  { to: '/agents', label: 'Agent', icon: IconAgent },
  { to: '/call-logs', label: 'Call Logs', icon: IconLogs },
  { to: '/users', label: 'Users', icon: IconUsers },
  { to: '/audit', label: 'Audit Log', icon: IconAudit },
]

const DEPT_NAV = [
  { to: '/', label: 'Dashboard', icon: IconDashboard, end: true },
  { to: '/tickets', label: 'Tickets', icon: IconTicket },
  { to: '/profile', label: 'My Profile', icon: IconUser },
]

function initials(name, username) {
  const s = (name || username || 'EPP').trim()
  const parts = s.split(/\s+/)
  return ((parts[0]?.[0] || '') + (parts[1]?.[0] || '')).toUpperCase() || 'EP'
}

export default function Layout() {
  const { user, isAdmin, logout } = useAuth()
  const navigate = useNavigate()
  const nav = isAdmin ? ADMIN_NAV : DEPT_NAV

  function doLogout() {
    logout()
    navigate('/login', { replace: true })
  }

  return (
    <div className="shell">
      <aside className="sidebar">
        <div className="brand">
          <div className="logo">EPP</div>
          <div>
            <div className="name">Support Helpline</div>
            <div className="sub">EPP Composites</div>
          </div>
        </div>
        <nav className="nav">
          {nav.map((n) => (
            <NavLink key={n.to} to={n.to} end={n.end} className={({ isActive }) => (isActive ? 'active' : '')}>
              <n.icon />
              <span>{n.label}</span>
            </NavLink>
          ))}
        </nav>
        <div className="sidebar-foot">
          <button className="btn ghost" style={{ width: '100%', display: 'flex', gap: 8, justifyContent: 'center' }} onClick={doLogout}>
            <IconLogout /> Sign out
          </button>
        </div>
      </aside>

      <div className="main">
        <header className="topbar">
          <div id="topbar-title" />
          <div style={{ display: 'flex', alignItems: 'center', gap: 14, marginLeft: 'auto' }}>
            <NavLink to="/profile" className="userchip" title="My profile">
              <div style={{ textAlign: 'right' }}>
                <div style={{ fontSize: '0.82rem', fontWeight: 600 }}>{user?.name || user?.username}</div>
                <div className="page-sub">{isAdmin ? 'Admin' : (user?.department_name || 'Department user')}</div>
              </div>
              <div className="avatar">{initials(user?.name, user?.username)}</div>
            </NavLink>
          </div>
        </header>
        <main className="content">
          <Outlet />
        </main>
      </div>
    </div>
  )
}
