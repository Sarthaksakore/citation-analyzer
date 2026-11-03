import React, { useState } from 'react'

export function SearchSection({
  onResolve,
  onAnalyze,
  onLoadCached,
  onDeleteCached,
  onClearAllCached,
  candidates,
  setCandidates,
  storedResearchers,
  loadingState,
  isResolving,
}) {
  const [query, setQuery] = useState('')
  const [showAdvanced, setShowAdvanced] = useState(false)
  const [maxPubs, setMaxPubs] = useState(null)
  const [maxPerSource, setMaxPerSource] = useState(null)

  const handleSubmit = (e) => {
    e.preventDefault()
    if (!query.trim()) return
    onResolve(query.trim())
  }

  const handleSelectExample = (val) => {
    setQuery(val)
    onResolve(val)
  }

  return (
    <div className="search-hero">
      <div className="hero-tag">
        <svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor">
          <path d="M12 2L15.09 8.26L22 9.27L17 14.14L18.18 21.02L12 17.77L5.82 21.02L7 14.14L2 9.27L8.91 8.26L12 2Z" />
        </svg>
        Multi-Database Academic Citation Verification
      </div>

      <h1 className="hero-title">
        Resolve & Disambiguate <span>Citations with High Precision</span>
      </h1>
      <p className="hero-desc">
        Unify citing records from OpenAlex, Semantic Scholar and OpenCitations. Eliminate duplicate counts and automatically isolate self-citations from external citations.
      </p>

      {/* Main Search Input */}
      <div className="search-box-card">
        <form className="search-form" onSubmit={handleSubmit}>
          <div className="search-input-wrapper">
            <svg className="search-input-icon" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
              <circle cx="11" cy="11" r="8" />
              <line x1="21" y1="21" x2="16.65" y2="16.65" />
            </svg>
            <input
              type="text"
              className="search-input"
              placeholder="Search by researcher name, ORCID (0000-...), or paper DOI (10.xxxx/...)"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              disabled={isResolving || loadingState.active}
            />
          </div>
          <button
            type="submit"
            className="btn-primary"
            disabled={!query.trim() || isResolving || loadingState.active}
          >
            {isResolving ? 'Searching...' : 'Find Researcher'}
          </button>
        </form>

        {/* Quick Example Chips */}
        <div className="quick-examples">
          <span>Try examples:</span>
          <button
            type="button"
            className="example-chip"
            onClick={() => handleSelectExample('Lokman Meho')}
          >
            Lokman Meho (Author)
          </button>
          <button
            type="button"
            className="example-chip"
            onClick={() => handleSelectExample('0000-0003-0623-4652')}
          >
            0000-0003-0623-4652 (ORCID)
          </button>
          <button
            type="button"
            className="example-chip"
            onClick={() => handleSelectExample('10.1002/asi.20416')}
          >
            10.1002/asi.20416 (DOI)
          </button>
        </div>

        {/* Advanced Options Toggle */}
        <button
          type="button"
          className="advanced-toggle"
          onClick={() => setShowAdvanced(!showAdvanced)}
        >
          <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" style={{ transform: showAdvanced ? 'rotate(90deg)' : 'none', transition: 'transform 0.15s ease' }}>
            <polyline points="9 18 15 12 9 6" />
          </svg>
          Pipeline Analysis Settings
        </button>

        {showAdvanced && (
          <div className="advanced-panel">
            <div className="config-item">
              <label className="config-label">Max Publications to Analyze</label>
              <select
                className="config-select"
                value={maxPubs ?? ''}
                onChange={(e) => setMaxPubs(e.target.value ? Number(e.target.value) : null)}
              >
                <option value={1}>1 Publication (Fastest)</option>
                <option value={2}>2 Publications</option>
                <option value={3}>3 Publications</option>
                <option value={5}>5 Publications</option>
                <option value={10}>10 Publications</option>
                <option value="">All available publications</option>
              </select>
            </div>
            <div className="config-item">
              <label className="config-label">Max Citations per Source</label>
              <select
                className="config-select"
                value={maxPerSource ?? ''}
                onChange={(e) => setMaxPerSource(e.target.value ? Number(e.target.value) : null)}
              >
                <option value={25}>25 Citations</option>
                <option value={50}>50 Citations</option>
                <option value={100}>100 Citations</option>
                <option value="">All available citations</option>
              </select>
            </div>
          </div>
        )}
      </div>

      {/* Loading Progress Box */}
      {loadingState.active && (
        <div className="loading-box">
          <div className="spinner" />
          <div className="loading-title">{loadingState.title || 'Running Citation Pipeline'}</div>
          <div className="loading-desc">{loadingState.message || 'Fetching raw records from OpenAlex & Semantic Scholar...'}</div>

          <div className="stepper">
            <div className={`step-item ${loadingState.step >= 1 ? (loadingState.step === 1 ? 'active' : 'completed') : ''}`}>
              <div className="step-icon">{loadingState.step > 1 ? '✓' : '1'}</div>
              <span>Layer 1: Identity Resolution & Disambiguation</span>
            </div>
            <div className={`step-item ${loadingState.step >= 2 ? (loadingState.step === 2 ? 'active' : 'completed') : ''}`}>
              <div className="step-icon">{loadingState.step > 2 ? '✓' : '2'}</div>
              <span>Layer 2: Multi-Source Citation Retrieval (OpenAlex,Semantic Scholar and OpenCitations)</span>
            </div>
            <div className={`step-item ${loadingState.step >= 3 ? (loadingState.step === 3 ? 'active' : 'completed') : ''}`}>
              <div className="step-icon">{loadingState.step > 3 ? '✓' : '3'}</div>
              <span>Layer 3: Cross-Source Deduplication & Title Alignment</span>
            </div>
            <div className={`step-item ${loadingState.step >= 4 ? (loadingState.step === 4 ? 'active' : 'completed') : ''}`}>
              <div className="step-icon">{loadingState.step > 4 ? '✓' : '4'}</div>
              <span>Layer 4: Self vs. External Citation Classification</span>
            </div>
            <div className={`step-item ${loadingState.step >= 5 ? (loadingState.step === 5 ? 'active' : 'completed') : ''}`}>
              <div className="step-icon">{loadingState.step > 5 ? '✓' : '5'}</div>
              <span>Layer 5: Generating Final Statistics & Verifying Data Integrity</span>
            </div>
          </div>
        </div>
      )}

      {/* Candidates List (from /researcher/resolve) */}
      {!loadingState.active && candidates && candidates.length > 0 && (
        <div className="candidates-container">
          <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: '4px' }}>
            <div className="section-heading" style={{ margin: 0 }}>
              <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                <path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2" />
                <circle cx="9" cy="7" r="4" />
                <path d="M23 21v-2a4 4 0 0 0-3-3.87" />
                <path d="M16 3.13a4 4 0 0 1 0 7.75" />
              </svg>
              Matching Researcher Profiles ({candidates.length})
            </div>
            {setCandidates && (
              <button
                type="button"
                className="btn-secondary"
                onClick={() => setCandidates([])}
                style={{ fontSize: '0.75rem', padding: '3px 8px', color: '#94a3b8' }}
              >
                Clear Search
              </button>
            )}
          </div>
          <div className="section-subheading">
            Select the intended profile below to run the multi-source citation analyzer.
          </div>

          <div className="candidates-grid">
            {candidates.map((cand) => (
              <div
                key={cand.openalex_id}
                className="candidate-card"
                onClick={() => onAnalyze(cand, { maxPubs, maxPerSource })}
              >
                <div>
                  <div className="candidate-name">{cand.display_name}</div>
                  <div className="candidate-meta">
                    <span className="candidate-id">ID: {cand.openalex_id}</span>
                    {cand.orcid && (
                      <span>ORCID: <strong style={{ color: '#86efac' }}>{cand.orcid.replace('https://orcid.org/', '')}</strong></span>
                    )}
                    {cand.works_count != null && (
                      <span>Works: {cand.works_count} publications</span>
                    )}
                  </div>
                </div>

                <div className="candidate-actions">
                  <button
                    type="button"
                    className="btn-primary"
                    style={{ width: '100%', justifyContent: 'center' }}
                    onClick={(e) => {
                      e.stopPropagation()
                      onAnalyze(cand, { maxPubs, maxPerSource })
                    }}
                  >
                    Run Citation Analysis
                  </button>
                </div>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Previously Analyzed Researchers (Database Cache) */}
      {!loadingState.active && storedResearchers?.length > 0 && (
        <div className="candidates-container">
          <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: '4px' }}>
            <div className="section-heading" style={{ margin: 0 }}>
              <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                <path d="M21 16V8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.73l7 4a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16z" />
                <polyline points="3.27 6.96 12 12.01 20.73 6.96" />
                <line x1="12" y1="22.08" x2="12" y2="12" />
              </svg>
              Locally Cached Profiles ({storedResearchers.length})
            </div>
            {onClearAllCached && (
              <button
                type="button"
                className="btn-secondary"
                onClick={() => {
                  if (window.confirm('Are you sure you want to clear all locally cached profiles?')) {
                    onClearAllCached()
                  }
                }}
                style={{ fontSize: '0.75rem', padding: '3px 8px', color: '#f87171', borderColor: 'rgba(239, 68, 68, 0.3)' }}
                title="Wipe all cached profiles"
              >
                Clear All
              </button>
            )}
          </div>
          <div className="section-subheading">
            Previously analyzed researchers available immediately without API round-trips.
          </div>

          <div className="candidates-grid">
            {storedResearchers.map((res) => (
              <div key={res.openalex_id || res.orcid} className="candidate-card">
                <div>
                  <div className="candidate-name">{res.display_name}</div>
                  <div className="candidate-meta">
                    <span className="candidate-id">ID: {res.openalex_id}</span>
                    {res.orcid && <span>ORCID: {res.orcid.replace('https://orcid.org/', '')}</span>}
                    {res.publication_count != null && <span>{res.publication_count} stored publications</span>}
                  </div>
                </div>
                <div className="candidate-actions" style={{ gap: '6px' }}>
                  <button
                    type="button"
                    className="btn-secondary"
                    onClick={() => onLoadCached(res.openalex_id, res)}
                    style={{ flex: 1, justifyContent: 'center' }}
                  >
                    View Cached
                  </button>
                  <button
                    type="button"
                    className="btn-primary"
                    onClick={() => onAnalyze(res, { maxPubs, maxPerSource })}
                    style={{ flex: 1, justifyContent: 'center' }}
                  >
                    Re-Analyze
                  </button>
                  {onDeleteCached && (
                    <button
                      type="button"
                      className="btn-secondary"
                      onClick={() => onDeleteCached(res.openalex_id || res.orcid)}
                      style={{ padding: '0 8px', color: '#f87171', borderColor: 'rgba(239, 68, 68, 0.25)' }}
                      title="Delete this cached profile"
                    >
                      <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                        <polyline points="3 6 5 6 21 6" />
                        <path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2" />
                      </svg>
                    </button>
                  )}
                </div>
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  )
}
