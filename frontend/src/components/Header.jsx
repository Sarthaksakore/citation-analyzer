import React from 'react'

export function Header({
  activeTab,
  setActiveTab,
  currentResearcher,
  unresolvedCount = 0,
  serverStatus,
}) {
  return (
    <header className="app-header">
      <div className="brand" onClick={() => setActiveTab('search')}>
        <div className="brand-icon">
          <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round">
            <path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20" />
            <path d="M6.5 2H20v20H6.5A2.5 2.5 0 0 1 4 19.5v-15A2.5 2.5 0 0 1 6.5 2z" />
            <path d="M8 7h8M8 11h6" />
          </svg>
        </div>
        <div>
          <div className="brand-title">Citation Analyzer</div>
          <div className="brand-subtitle">Multi-Source Verification & Disambiguation</div>
        </div>
      </div>

      <div className="header-center">
        <button
          type="button"
          className={`nav-tab ${activeTab === 'search' ? 'active' : ''}`}
          onClick={() => setActiveTab('search')}
        >
          <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2">
            <circle cx="11" cy="11" r="8" />
            <line x1="21" y1="21" x2="16.65" y2="16.65" />
          </svg>
          Search
        </button>

        {currentResearcher && (
          <>
            <button
              type="button"
              className={`nav-tab ${activeTab === 'dashboard' ? 'active' : ''}`}
              onClick={() => setActiveTab('dashboard')}
            >
              <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2">
                <rect x="3" y="3" width="7" height="9" />
                <rect x="14" y="3" width="7" height="5" />
                <rect x="14" y="12" width="7" height="9" />
                <rect x="3" y="16" width="7" height="5" />
              </svg>
              Dashboard
            </button>

            <button
              type="button"
              className={`nav-tab ${activeTab === 'citations' ? 'active' : ''}`}
              onClick={() => setActiveTab('citations')}
            >
              <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2">
                <line x1="8" y1="6" x2="21" y2="6" />
                <line x1="8" y1="12" x2="21" y2="12" />
                <line x1="8" y1="18" x2="21" y2="18" />
                <line x1="3" y1="6" x2="3.01" y2="6" />
                <line x1="3" y1="12" x2="3.01" y2="12" />
                <line x1="3" y1="18" x2="3.01" y2="18" />
              </svg>
              Citations
            </button>

            <button
              type="button"
              className={`nav-tab ${activeTab === 'review' ? 'active' : ''}`}
              onClick={() => setActiveTab('review')}
            >
              <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2">
                <path d="M16 4h2a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2h2" />
                <rect x="8" y="2" width="8" height="4" rx="1" ry="1" />
                <path d="M9 14l2 2 4-4" />
              </svg>
              Review Queue
              {unresolvedCount > 0 && (
                <span className="nav-badge">{unresolvedCount}</span>
              )}
            </button>
          </>
        )}
      </div>

      <div className="header-right">
        <div className="server-status" title={`FastAPI Backend at http://localhost:8000`}>
          <span className={`status-dot ${serverStatus === 'connected' ? '' : 'offline'}`} />
          <span>{serverStatus === 'connected' ? 'Backend Online' : 'Connecting...'}</span>
        </div>
      </div>
    </header>
  )
}
