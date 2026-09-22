import { createContext, useContext, useEffect, useState } from 'react'
import { api, getToken, setToken } from './api.js'

const AuthCtx = createContext(null)

const NO_UI = { hidden_pages: [] }

export function AuthProvider({ children }) {
  const [user, setUser] = useState(null)
  const [ui, setUi] = useState(NO_UI)          // server-side UI config: which admin pages are hidden
  const [ready, setReady] = useState(false)

  useEffect(() => {
    let cancelled = false
    async function boot() {
      if (!getToken()) { setReady(true); return }
      try {
        const r = await api.get('/me')
        if (!cancelled) { setUser(r.user); setUi(r.ui || NO_UI) }
      } catch {
        setToken('')
      } finally {
        if (!cancelled) setReady(true)
      }
    }
    boot()
    return () => { cancelled = true }
  }, [])

  async function login(username, password) {
    const r = await api.post('/login', { username, password })
    setToken(r.token)
    setUser(r.user)
    setUi(r.ui || NO_UI)
    return r.user
  }

  function logout() {
    api.post('/logout').catch(() => {})
    setToken('')
    setUser(null)
  }

  const hidden = new Set(ui?.hidden_pages || [])
  return (
    <AuthCtx.Provider value={{ user, ready, login, logout, isAdmin: user?.role === 'admin', ui,
                               isHidden: (page) => !!page && hidden.has(page) }}>
      {children}
    </AuthCtx.Provider>
  )
}

export function useAuth() {
  return useContext(AuthCtx)
}
