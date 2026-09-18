import { Routes, Route, Navigate, useLocation } from 'react-router-dom'
import { useAuth } from './auth.jsx'
import Layout from './components/Layout.jsx'
import Login from './pages/Login.jsx'
import Dashboard from './pages/Dashboard.jsx'
import Tickets from './pages/Tickets.jsx'
import TicketDetail from './pages/TicketDetail.jsx'
import Routing from './pages/Routing.jsx'
import Agents from './pages/Agents.jsx'
import AgentEditor from './pages/AgentEditor.jsx'
import CallLogsPage from './pages/CallLogsPage.jsx'
import Users from './pages/Users.jsx'
import AuditLog from './pages/AuditLog.jsx'
import Profile from './pages/Profile.jsx'

function Protected({ children, adminOnly }) {
  const { user, ready, isAdmin } = useAuth()
  const loc = useLocation()
  if (!ready) return <div className="center">Loading…</div>
  if (!user) return <Navigate to="/login" replace state={{ from: loc.pathname }} />
  if (adminOnly && !isAdmin) return <Navigate to="/" replace />
  return children
}

export default function App() {
  return (
    <Routes>
      <Route path="/login" element={<Login />} />
      <Route element={<Protected><Layout /></Protected>}>
        <Route path="/" element={<Dashboard />} />
        <Route path="/tickets" element={<Tickets />} />
        <Route path="/tickets/:id" element={<TicketDetail />} />
        <Route path="/routing" element={<Protected adminOnly><Routing /></Protected>} />
        <Route path="/agents" element={<Protected adminOnly><Agents /></Protected>} />
        <Route path="/agents/:id" element={<Protected adminOnly><AgentEditor /></Protected>} />
        <Route path="/call-logs" element={<Protected adminOnly><CallLogsPage /></Protected>} />
        <Route path="/users" element={<Protected adminOnly><Users /></Protected>} />
        <Route path="/audit" element={<Protected adminOnly><AuditLog /></Protected>} />
        <Route path="/profile" element={<Profile />} />
      </Route>
      <Route path="*" element={<Navigate to="/" replace />} />
    </Routes>
  )
}
