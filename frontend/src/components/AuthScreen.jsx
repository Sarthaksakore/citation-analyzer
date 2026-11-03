import React, { useState } from 'react'
import { api } from '../api'

export function AuthScreen({ onAuthenticated }) {
  const [mode, setMode] = useState('login')
  const [form, setForm] = useState({ fullName: '', email: '', password: '', confirmPassword: '' })
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(false)
  const [showPassword, setShowPassword] = useState(false)
  const [showConfirmPassword, setShowConfirmPassword] = useState(false)

  const update = (field) => (event) => {
    setForm((current) => ({ ...current, [field]: event.target.value }))
    setError('')
  }

  const passwordChecks = {
    length: form.password.length >= 8,
    letter: /[A-Za-z]/.test(form.password),
    number: /\d/.test(form.password),
  }
  const strength = Object.values(passwordChecks).filter(Boolean).length
  const passwordsMatch = form.password.length > 0 && form.password === form.confirmPassword

  const submit = async (event) => {
    event.preventDefault()
    setError('')
    if (mode === 'register' && (strength < 3 || !passwordsMatch)) {
      setError(!passwordsMatch ? 'Passwords do not match.' : 'Choose a password that meets all requirements.')
      return
    }
    setLoading(true)
    try {
      const payload = mode === 'login'
        ? { email: form.email, password: form.password }
        : {
          full_name: form.fullName,
          email: form.email,
          password: form.password,
          confirm_password: form.confirmPassword,
        }
      const data = mode === 'login' ? await api.login(payload) : await api.register(payload)
      onAuthenticated(data.user)
    } catch (err) {
      setError(err.message || 'Authentication failed.')
    } finally {
      setLoading(false)
    }
  }

  return (
    <main className="auth-page">
      <section className="auth-card">
        <div className="auth-brand">
          <div className="brand-icon auth-icon">V</div>
          <span>CITATION ANALYZER</span>
        </div>
        <h1>{mode === 'login' ? 'Welcome back' : 'Create your account'}</h1>
        <p className="auth-subtitle">
          {mode === 'login' ? 'Sign in to open your Citation Analyzer dashboard.' : 'Register with your email to start analyzing citations.'}
        </p>
        <form onSubmit={submit} className="auth-form">
          {mode === 'register' && (
            <label>
              <span>Full Name</span>
              <input value={form.fullName} onChange={update('fullName')} autoComplete="name" placeholder="e.g. Vidya Gaikwad" required />
            </label>
          )}
          <label>
            <span>Email Address</span>
            <input type="email" value={form.email} onChange={update('email')} autoComplete="email" placeholder="you@example.com" required />
            <small>Use an email address you can access.</small>
          </label>
          <label>
            <span>Password</span>
            <div className="password-input">
              <input type={showPassword ? 'text' : 'password'} value={form.password} onChange={update('password')} autoComplete={mode === 'login' ? 'current-password' : 'new-password'} minLength="8" required />
              <button type="button" className="password-toggle" onClick={() => setShowPassword((value) => !value)} aria-label={showPassword ? 'Hide password' : 'Show password'}>
                {showPassword ? 'Hide' : 'Show'}
              </button>
            </div>
          </label>
          {mode === 'register' && (
            <>
              <div className="password-guidance">
                <div className="strength-label">Password strength: <strong>{strength === 3 ? 'Strong' : strength === 2 ? 'Good' : 'Needs work'}</strong></div>
                <div className="strength-bar"><span style={{ width: `${(strength / 3) * 100}%` }} /></div>
                <ul>
                  <li className={passwordChecks.length ? 'valid' : ''}>At least 8 characters</li>
                  <li className={passwordChecks.letter ? 'valid' : ''}>One letter</li>
                  <li className={passwordChecks.number ? 'valid' : ''}>One number</li>
                </ul>
              </div>
              <label>
                <span>Confirm Password</span>
                <div className="password-input">
                  <input type={showConfirmPassword ? 'text' : 'password'} value={form.confirmPassword} onChange={update('confirmPassword')} autoComplete="new-password" minLength="8" required />
                  <button type="button" className="password-toggle" onClick={() => setShowConfirmPassword((value) => !value)} aria-label={showConfirmPassword ? 'Hide confirmation password' : 'Show confirmation password'}>
                    {showConfirmPassword ? 'Hide' : 'Show'}
                  </button>
                </div>
                {form.confirmPassword && <small className={passwordsMatch ? 'field-valid' : 'field-invalid'}>{passwordsMatch ? 'Passwords match' : 'Passwords do not match'}</small>}
              </label>
            </>
          )}
          {error && <div className="auth-error" role="alert">{error}</div>}
          <button type="submit" className="btn-primary auth-submit" disabled={loading}>
            {loading ? 'Please wait...' : mode === 'login' ? 'Log In to Dashboard' : 'Create Account'}
          </button>
        </form>
        <div className="auth-security-note">Your password is encrypted and never stored in your browser.</div>
        <button type="button" className="auth-switch" onClick={() => { setMode(mode === 'login' ? 'register' : 'login'); setError('') }}>
          {mode === 'login' ? 'Need an account? Register' : 'Already registered? Log in'}
        </button>
      </section>
    </main>
  )
}
