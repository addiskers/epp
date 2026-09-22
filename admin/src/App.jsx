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
import Contacts from './pages/Contacts.jsx'
import Campaigns from './pages/Campaigns.jsx'
import CampaignDetail from './pages/CampaignDetail.jsx'
import CreateCampaign from './pages/CreateCampaign.jsx'
import Scheduler from './pages/Scheduler.jsx'
import Subscription from './pages/Subscription.jsx'

// `page` names an admin page the server may hide (EPP_HIDDEN_PAGES); a hidden page is
// unreachable by URL as well as missing from the menu.
function Protected({ children, adminOnly, page }) {
  const { user, ready, isAdmin, isHidden } = useAuth()
  const loc = useLocation()
  if (!ready) return <div className="center">Loading…</div>
  if (!user) return <Navigate to="/login" replace state={{ from: loc.pathname }} />
  if (adminOnly && !isAdmin) return <Navigate to="/" replace />
  if (isHidden(page)) return <Navigate to="/" replace />
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
        <Route path="/contacts" element={<Protected adminOnly page="contacts"><Contacts /></Protected>} />
        <Route path="/campaigns" element={<Protected adminOnly page="campaigns"><Campaigns /></Protected>} />
        <Route path="/campaigns/new" element={<Protected adminOnly page="campaigns"><CreateCampaign /></Protected>} />
        <Route path="/campaigns/:id" element={<Protected adminOnly page="campaigns"><CampaignDetail /></Protected>} />
        <Route path="/scheduler" element={<Protected adminOnly page="scheduler"><Scheduler /></Protected>} />
        <Route path="/routing" element={<Protected adminOnly page="routing"><Routing /></Protected>} />
        <Route path="/agents" element={<Protected adminOnly page="agents"><Agents /></Protected>} />
        <Route path="/agents/:id" element={<Protected adminOnly page="agents"><AgentEditor /></Protected>} />
        <Route path="/call-logs" element={<Protected adminOnly page="call-logs"><CallLogsPage /></Protected>} />
        <Route path="/users" element={<Protected adminOnly page="users"><Users /></Protected>} />
        <Route path="/audit" element={<Protected adminOnly page="audit"><AuditLog /></Protected>} />
        <Route path="/subscription" element={<Protected adminOnly page="subscription"><Subscription /></Protected>} />
        <Route path="/profile" element={<Profile />} />
      </Route>
      <Route path="*" element={<Navigate to="/" replace />} />
    </Routes>
  )
}
