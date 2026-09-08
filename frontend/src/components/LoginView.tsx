import { useState } from 'react'
import { AppError, login } from '../api'

interface LoginViewProps {
  onLoggedIn: () => void
}

// No self-service signup or password reset here, deliberately -- accounts
// are created only via scripts/create_org.py + scripts/create_user.py
// (see backend/auth.py's module docstring), so this is just username +
// password, no "forgot password" or "sign up" link to build or maintain.
export default function LoginView({ onLoggedIn }: LoginViewProps) {
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [submitting, setSubmitting] = useState(false)

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault()
    setError(null)
    setSubmitting(true)
    try {
      const result = await login(username, password)
      if (result === false) {
        setError('Incorrect username or password.')
        return
      }
      onLoggedIn()
    } catch (err) {
      setError(err instanceof AppError ? err.message : String(err))
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <div className="app-layout">
      <main className="main-content">
        <div className="app-shell">
          <div className="card" style={{ maxWidth: 360, margin: '80px auto' }}>
            <div className="app-header" style={{ marginBottom: 20 }}>
              <span className="app-mark" aria-hidden="true">FH</span>
              <h1 className="app-title">Find My Highlights</h1>
            </div>
            <form onSubmit={handleSubmit}>
              <p>
                <label htmlFor="login-username">Username</label>
                <br />
                <input
                  id="login-username"
                  type="text"
                  value={username}
                  onChange={(e) => setUsername(e.target.value)}
                  autoFocus
                  required
                />
              </p>
              <p>
                <label htmlFor="login-password">Password</label>
                <br />
                <input
                  id="login-password"
                  type="password"
                  value={password}
                  onChange={(e) => setPassword(e.target.value)}
                  required
                />
              </p>
              {error && <p className="alert alert-danger">{error}</p>}
              <p>
                <button type="submit" disabled={submitting}>
                  {submitting ? 'Signing in...' : 'Sign in'}
                </button>
              </p>
            </form>
          </div>
        </div>
      </main>
    </div>
  )
}
