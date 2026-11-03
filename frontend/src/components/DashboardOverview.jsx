import React, { useState } from 'react'
import { api } from '../api'

export function DashboardOverview({
  analysisData,
  onNavigateTab,
}) {
  const [verifying, setVerifying] = useState(false)
  const [integrityStatus, setIntegrityStatus] = useState(null)

  if (!analysisData) return null

  const { researcher, dedup_summary, statistics, citations = [], publications_processed } = analysisData
  const googleScholar = analysisData.google_scholar
  const googleScholarTotal = googleScholar?.citation_metrics?.all ?? googleScholar?.citation_metrics?.search_results
  const googleScholarScope = googleScholar?.citation_metrics?.all != null
    ? 'profile citations'
    : 'matching search-result citations'
  const googleScholarCitingCount = googleScholar?.citing_paper_count ?? 0
  const googleScholarRawCitingCount = googleScholar?.raw_citing_paper_count ?? googleScholarCitingCount
  const googleScholarSelfCount = googleScholar?.self_citation_count ?? 0
  const googleScholarArticleCount = googleScholar?.profile_article_count
  const providerStatus = analysisData.provider_status || {}
  const incompleteProviders = Object.entries(providerStatus)
    .filter(([, status]) => status?.status && status.status !== 'complete')
  const dataIncomplete = incompleteProviders.length > 0

  // Citations classification stats
  const totalDeduped = statistics?.deduplicated_count ?? citations.length
  const selfCount = statistics?.self_citation_count ?? citations.filter(c => c.classification === 'SELF').length
  const externalCount = statistics?.external_citation_count ?? citations.filter(c => c.classification === 'EXTERNAL').length
  const unresolvedCount = statistics?.unresolved_count ?? citations.filter(c => c.classification === 'UNRESOLVED' || c.needs_review).length

  const rawCount = statistics?.total_raw ?? analysisData.raw_citation_count ?? totalDeduped
  const dupesRemoved = (dedup_summary?.duplicates_removed && dedup_summary.duplicates_removed > 0)
    ? dedup_summary.duplicates_removed
    : Math.max(rawCount - totalDeduped, 0)
  const dedupPercent = rawCount > 0 ? ((dupesRemoved / rawCount) * 100).toFixed(1) : 0

  const selfPct = totalDeduped > 0 ? ((selfCount / totalDeduped) * 100).toFixed(1) : 0
  const extPct = totalDeduped > 0 ? ((externalCount / totalDeduped) * 100).toFixed(1) : 0
  const unresPct = totalDeduped > 0 ? ((unresolvedCount / totalDeduped) * 100).toFixed(1) : 0

  // Sources breakdown
  const sourceWise = statistics?.raw_source_counts || {
    openalex: citations.filter(c => (c.sources || []).includes('openalex')).length,
    semantic_scholar: citations.filter(c => (c.sources || []).includes('semantic_scholar')).length,
    opencitations: citations.filter(c => (c.sources || []).includes('opencitations')).length,
    google_scholar: citations.filter(c => (c.sources || []).includes('google_scholar')).length,
  }

  const maxSourceCount = Math.max(...Object.values(sourceWise), 1)

  // Donut chart stroke calculations
  const radius = 55
  const circumference = 2 * Math.PI * radius // ~345.575
  const selfLength = (selfCount / (totalDeduped || 1)) * circumference
  const extLength = (externalCount / (totalDeduped || 1)) * circumference
  const unresLength = (unresolvedCount / (totalDeduped || 1)) * circumference

  const handleVerifyIntegrity = async () => {
    if (!statistics) return
    setVerifying(true)
    setIntegrityStatus(null)
    try {
      const res = await api.verifyIntegrity(statistics)
      setIntegrityStatus(res.valid ? 'valid' : 'invalid')
    } catch (err) {
      setIntegrityStatus('invalid')
    } finally {
      setVerifying(false)
    }
  }

  return (
    <div className="dashboard-container">
      {/* Profile Header Banner */}
      <div className="profile-banner">
        <div className="profile-info">
          <div className="profile-avatar">
            {researcher?.display_name?.charAt(0) || 'R'}
          </div>
          <div>
            <h2 className="profile-name">{researcher?.display_name || 'Author Profile'}</h2>
            <div className="profile-meta-tags">
              <span className="candidate-id">ID: {researcher?.openalex_id}</span>
              {researcher?.orcid && (
                <span>ORCID: <strong>{researcher.orcid.replace('https://orcid.org/', '')}</strong></span>
              )}
            </div>
          </div>
        </div>

        <div className="profile-actions">
          {unresolvedCount > 0 && (
            <button
              type="button"
              className="btn-primary"
              style={{ background: 'linear-gradient(135deg, #f59e0b, #d97706)' }}
              onClick={() => onNavigateTab('review')}
            >
              Review Queue ({unresolvedCount})
            </button>
          )}
          <button
            type="button"
            className="btn-secondary"
            onClick={() => onNavigateTab('citations')}
          >
            View All Citations ({citations.length})
          </button>
        </div>

        {incompleteProviders.length > 0 && (
          <div
            role="status"
            style={{
              marginTop: '1rem',
              padding: '0.85rem 1rem',
              border: '1px solid rgba(245, 158, 11, 0.45)',
              borderRadius: '0.75rem',
              background: 'rgba(120, 53, 15, 0.18)',
              color: '#fde68a',
              fontSize: '0.85rem',
            }}
          >
            <strong>Data completeness warning</strong>
            <ul style={{ margin: '0.45rem 0 0 1.1rem' }}>
              {incompleteProviders.map(([provider, status]) => (
                <li key={provider}>
                  {provider}: {status.note || 'some records could not be fetched or completeness could not be confirmed'}
                  {status.fetch?.clusters_failed > 0 && ` (${status.fetch.clusters_failed} citation groups failed)`}
                </li>
              ))}
            </ul>
            <div style={{ marginTop: '0.45rem', color: '#fef3c7' }}>
              Counts shown below are fetched records, not an estimate of missing records.
            </div>
          </div>
        )}
      </div>

      {/* Primary KPI Metrics */}
      <div className="metrics-grid">
        <div className="metric-card">
          <div className="metric-label">Analyzer Citations Retrieved</div>
          <div className="metric-value">{rawCount}</div>
          <div className="metric-sub">Across analyzed publications and supported sources</div>
        </div>

        <div className="metric-card">
          <div className="metric-label">Unique Analyzer Citations</div>
          <div className="metric-value">{totalDeduped}</div>
          <div className="metric-sub">
            <strong style={{ color: '#86efac' }}>{dupesRemoved} duplicates removed</strong> ({dedupPercent}%)
          </div>
        </div>

        <div className="metric-card emerald self-highlight">
          <div className="metric-label">Self-Citations</div>
          <div className="metric-value" style={{ color: 'var(--color-self)' }}>{dataIncomplete ? '—' : selfCount}</div>
          <div className="metric-sub self-highlight-sub">
          {dataIncomplete ? (
            <span>Cannot confirm while provider data is incomplete</span>
          ) : (
            <>
              <strong>{selfPct}%</strong> of verified citations
              <span>{selfCount > 0 ? 'Identity evidence confirmed' : 'No confirmed author matches'}</span>
            </>
          )}
          </div>
        </div>

        <div className="metric-card sky">
          <div className="metric-label">{dataIncomplete ? 'Provisional External Citations' : 'External Citations'}</div>
          <div className="metric-value" style={{ color: 'var(--color-external)' }}>{externalCount}</div>
          <div className="metric-sub">{dataIncomplete ? 'Not a complete peer-impact measure' : `${extPct}% genuine peer impact`}</div>
        </div>

        <div className="metric-card amber">
          <div className="metric-label">Needs Review / Unresolved</div>
          <div className="metric-value" style={{ color: 'var(--color-unresolved)' }}>{unresolvedCount}</div>
          <div className="metric-sub">
            {unresolvedCount > 0 ? (
              <a href="#review" onClick={(e) => { e.preventDefault(); onNavigateTab('review'); }} style={{ color: '#fcd34d' }}>
                Resolve in Review Queue &rarr;
              </a>
            ) : (
              'All citations disambiguated'
            )}
          </div>
        </div>
      </div>

      {/* Analytics Breakdown Row: SVG Donut Chart + Source Breakdown */}
      <div className="analytics-row">
        {/* SVG Donut Breakdown Chart */}
        <div className="dashboard-card">
          <div className="card-header">
            <h3 className="card-title">
              <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                <path d="M21.21 15.89A10 10 0 1 1 8 2.83" />
                <path d="M22 12A10 10 0 0 0 12 2v10z" />
              </svg>
              Citation Classification Distribution
            </h3>

          </div>

          <div className="chart-content">
            <div className="donut-svg-wrap">
              <svg width="170" height="170" viewBox="0 0 140 140">
                <circle
                  cx="70"
                  cy="70"
                  r={radius}
                  fill="none"
                  stroke="rgba(255, 255, 255, 0.05)"
                  strokeWidth="16"
                />
                {totalDeduped > 0 ? (
                  <>
                    {/* Self Segment */}
                    <circle
                      cx="70"
                      cy="70"
                      r={radius}
                      fill="none"
                      stroke="var(--color-self)"
                      strokeWidth="16"
                      strokeDasharray={`${selfLength} ${circumference - selfLength}`}
                      strokeDashoffset="0"
                      transform="rotate(-90 70 70)"
                    />
                    {/* External Segment */}
                    <circle
                      cx="70"
                      cy="70"
                      r={radius}
                      fill="none"
                      stroke="var(--color-external)"
                      strokeWidth="16"
                      strokeDasharray={`${extLength} ${circumference - extLength}`}
                      strokeDashoffset={-selfLength}
                      transform="rotate(-90 70 70)"
                    />
                    {/* Unresolved Segment */}
                    <circle
                      cx="70"
                      cy="70"
                      r={radius}
                      fill="none"
                      stroke="var(--color-unresolved)"
                      strokeWidth="16"
                      strokeDasharray={`${unresLength} ${circumference - unresLength}`}
                      strokeDashoffset={-(selfLength + extLength)}
                      transform="rotate(-90 70 70)"
                    />
                  </>
                ) : null}
              </svg>
              <div className="donut-center-text">
                <div className="donut-center-number">{totalDeduped}</div>
                <div className="donut-center-label">Unique</div>
              </div>
            </div>

            <div className="chart-legend">
              <div className="legend-item">
                <div className="legend-left">
                  <span className="legend-color self" />
                  <span>SELF</span>
                </div>
                <span className="legend-value">{selfCount} ({selfPct}%)</span>
              </div>
              <div className="legend-item">
                <div className="legend-left">
                  <span className="legend-color external" />
                  <span>EXTERNAL</span>
                </div>
                <span className="legend-value">{externalCount} ({extPct}%)</span>
              </div>
              <div className="legend-item">
                <div className="legend-left">
                  <span className="legend-color unresolved" />
                  <span>UNRESOLVED</span>
                </div>
                <span className="legend-value">{unresolvedCount} ({unresPct}%)</span>
              </div>
            </div>
          </div>
        </div>

        {/* Source Distribution Breakdown */}
        <div className="dashboard-card">
          <div className="card-header">
            <h3 className="card-title">
              <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                <rect x="2" y="2" width="20" height="8" rx="2" ry="2" />
                <rect x="2" y="14" width="20" height="8" rx="2" ry="2" />
                <line x1="6" y1="6" x2="6.01" y2="6" />
                <line x1="6" y1="18" x2="6.01" y2="18" />
              </svg>
              Multi-Source Pipeline Coverage
            </h3>
          </div>

          <div className="source-list">
            <div className="source-row">
              <div className="source-row-info">
                <span>OpenAlex</span>
                <strong>{sourceWise.openalex || 0} citations</strong>
              </div>
              <div className="source-bar-bg">
                <div
                  className="source-bar-fill"
                  style={{
                    width: `${((sourceWise.openalex || 0) / maxSourceCount) * 100}%`,
                    background: 'var(--source-openalex)',
                  }}
                />
              </div>
            </div>

            <div className="source-row">
              <div className="source-row-info">
                <span>Semantic Scholar</span>
                <strong>{sourceWise.semantic_scholar || 0} citations</strong>
              </div>
              <div className="source-bar-bg">
                <div
                  className="source-bar-fill"
                  style={{
                    width: `${((sourceWise.semantic_scholar || 0) / maxSourceCount) * 100}%`,
                    background: 'var(--source-s2)',
                  }}
                />
              </div>
            </div>

            <div className="source-row">
              <div className="source-row-info">
                <span>OpenCitations (COCI)</span>
                <strong>{sourceWise.opencitations || 0} citations</strong>
              </div>
              <div className="source-bar-bg">
                <div
                  className="source-bar-fill"
                  style={{
                    width: `${((sourceWise.opencitations || 0) / maxSourceCount) * 100}%`,
                    background: 'var(--source-opencitations)',
                  }}
                />
              </div>
            </div>

            <div className="source-row">
              <div className="source-row-info">
                <span>Google Scholar papers analyzed</span>
                <strong>{sourceWise.google_scholar || 0} papers</strong>
              </div>
              <div className="source-bar-bg">
                <div
                  className="source-bar-fill"
                  style={{
                    width: `${((sourceWise.google_scholar || 0) / maxSourceCount) * 100}%`,
                    background: 'var(--source-google-scholar, #f59e0b)',
                  }}
                />
              </div>
            </div>

            {dedup_summary?.cross_source_citations != null && (
              <div style={{ marginTop: '0.75rem', fontSize: '0.8rem', color: 'var(--text-secondary)' }}>
                <span>Cross-source corroborated citations: <strong>{dedup_summary.cross_source_citations}</strong></span>
              </div>
            )}

            <div className="source-row" style={{ marginTop: '0.75rem' }}>
              <div className="source-row-info">
                <span>Google Scholar profile total</span>
                <strong>
                  {googleScholar?.matched && googleScholarTotal != null
                    ? `${googleScholarTotal} ${googleScholarScope}`
                    : googleScholar?.enabled
                      ? 'No profile match'
                      : 'Provider not configured'}
                </strong>
              </div>
              {googleScholar?.matched && googleScholar.profile_url && (
                <a href={googleScholar.profile_url} target="_blank" rel="noreferrer" style={{ color: 'var(--text-secondary)', fontSize: '0.8rem' }}>
                  {googleScholar.profile_name}
                </a>
              )}
              {googleScholarCitingCount > 0 && (
                <span style={{ color: 'var(--text-secondary)', fontSize: '0.8rem' }}>
                  {googleScholarCitingCount} unique papers analyzed ({googleScholarRawCitingCount} raw records)
                </span>
              )}
              {googleScholar?.matched && googleScholarTotal != null && googleScholarCitingCount > 0 && (
                <span style={{ color: 'var(--text-muted)', fontSize: '0.72rem' }}>
                  Profile total counts citation occurrences; fetched papers are deduplicated.
                </span>
              )}
              {googleScholar?.matched && googleScholarArticleCount != null && (
                <span style={{ color: 'var(--text-secondary)', fontSize: '0.8rem' }}>
                  {googleScholarArticleCount} profile articles fetched
                </span>
              )}
              {googleScholar?.matched && (
                <span style={{ color: 'var(--color-self)', fontSize: '0.8rem', fontWeight: 700 }}>
                  Scholar self-citations: {googleScholarSelfCount}
                </span>
              )}
            </div>
          </div>
        </div>
      </div>

      {/* Layer 5: Integrity & Tamper Protection Card */}
      {statistics?._integrity && (
        <div className="dashboard-card" style={{ marginBottom: '2rem' }}>
          <div className="card-header">
            <h3 className="card-title">
              <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                <path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z" />
              </svg>
              Cryptographic Integrity Verification (HMAC-SHA256)
            </h3>
            <button
              type="button"
              className="btn-secondary"
              onClick={handleVerifyIntegrity}
              disabled={verifying}
              style={{ fontSize: '0.8rem', padding: '0.35rem 0.75rem' }}
            >
              {verifying ? 'Verifying...' : 'Validate Checksum'}
            </button>
          </div>

          <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', flexWrap: 'wrap', gap: '1rem' }}>
            <div style={{ fontSize: '0.82rem', color: 'var(--text-secondary)' }}>
              Summary Signature: <code style={{ color: '#a5b4fc', fontFamily: 'var(--font-mono)' }}>
                {typeof statistics._integrity === 'object' ? statistics._integrity.checksum : statistics._integrity}
              </code>
            </div>

            {integrityStatus === 'valid' && (
              <div className="badge badge-self">
                ✓ Checksum Authenticated
              </div>
            )}
            {integrityStatus === 'invalid' && (
              <div className="badge" style={{ background: 'rgba(239, 68, 68, 0.15)', color: '#f87171', border: '1px solid #ef4444' }}>
                ✕ Checksum Mismatch
              </div>
            )}
          </div>
        </div>
      )}
    </div>
  )
}
