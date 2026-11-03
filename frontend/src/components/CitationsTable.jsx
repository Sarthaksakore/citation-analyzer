import React, { useState, useMemo } from 'react'

export function CitationsTable({
  citations = [],
  onReviewCitation,
}) {
  const PAGE_SIZE = 100
  const [filterClass, setFilterClass] = useState('ALL')
  const [searchQuery, setSearchQuery] = useState('')
  const [sortField, setSortField] = useState('year')
  const [sortOrder, setSortOrder] = useState('desc') // 'asc' | 'desc'
  const [page, setPage] = useState(0)

  const filteredCitations = useMemo(() => {
    return citations.filter((cit) => {
      // Classification filter
      if (filterClass !== 'ALL') {
        if (filterClass === 'NEEDS_REVIEW' && !cit.needs_review) return false
        if (filterClass !== 'NEEDS_REVIEW' && cit.classification !== filterClass) return false
      }

      // Text search
      if (searchQuery.trim()) {
        const q = searchQuery.toLowerCase()
        const titleMatch = (cit.title || '').toLowerCase().includes(q)
        const authorMatch = (cit.authors || []).some(a => a.toLowerCase().includes(q))
        const doiMatch = (cit.doi || '').toLowerCase().includes(q)
        const venueMatch = (cit.venue || '').toLowerCase().includes(q)
        if (!titleMatch && !authorMatch && !doiMatch && !venueMatch) return false
      }

      return true
    })
  }, [citations, filterClass, searchQuery])

  const sortedCitations = useMemo(() => {
    return [...filteredCitations].sort((a, b) => {
      let valA = a[sortField] ?? ''
      let valB = b[sortField] ?? ''

      if (sortField === 'year' || sortField === 'confidence' || sortField === 'duplicate_count') {
        valA = Number(valA) || 0
        valB = Number(valB) || 0
      }

      if (valA < valB) return sortOrder === 'asc' ? -1 : 1
      if (valA > valB) return sortOrder === 'asc' ? 1 : -1
      return 0
    })
  }, [filteredCitations, sortField, sortOrder])

  const pageCount = Math.ceil(sortedCitations.length / PAGE_SIZE)
  const visibleCitations = sortedCitations.slice(
    page * PAGE_SIZE,
    (page + 1) * PAGE_SIZE,
  )

  const handleSort = (field) => {
    setPage(0)
    if (sortField === field) {
      setSortOrder(sortOrder === 'asc' ? 'desc' : 'asc')
    } else {
      setSortField(field)
      setSortOrder(field === 'year' || field === 'confidence' ? 'desc' : 'asc')
    }

    const updateFilter = (value) => {
      setPage(0)
      setFilterClass(value)
    }
  }

  const renderSortIndicator = (field) => {
    if (sortField !== field) return null
    return sortOrder === 'asc' ? ' ▲' : ' ▼'
  }

  return (
    <div className="table-card">
      <div className="table-toolbar">
        <div className="table-filter-group">
          <button
            type="button"
            className={`filter-btn ${filterClass === 'ALL' ? 'active' : ''}`}
            onClick={() => updateFilter('ALL')}
          >
            All ({citations.length})
          </button>
          <button
            type="button"
            className={`filter-btn ${filterClass === 'SELF' ? 'active' : ''}`}
            onClick={() => updateFilter('SELF')}
          >
            Self ({citations.filter(c => c.classification === 'SELF').length})
          </button>
          <button
            type="button"
            className={`filter-btn ${filterClass === 'EXTERNAL' ? 'active' : ''}`}
            onClick={() => updateFilter('EXTERNAL')}
          >
            External ({citations.filter(c => c.classification === 'EXTERNAL').length})
          </button>
          <button
            type="button"
            className={`filter-btn ${filterClass === 'UNRESOLVED' ? 'active' : ''}`}
            onClick={() => updateFilter('UNRESOLVED')}
          >
            Unresolved ({citations.filter(c => c.classification === 'UNRESOLVED').length})
          </button>
          <button
            type="button"
            className={`filter-btn ${filterClass === 'NEEDS_REVIEW' ? 'active' : ''}`}
            onClick={() => updateFilter('NEEDS_REVIEW')}
          >
            Needs Review ({citations.filter(c => c.needs_review).length})
          </button>
        </div>

        <input
          type="text"
          className="table-search-input"
          placeholder="Filter citations by title, author, DOI..."
          value={searchQuery}
          onChange={(e) => {
            setPage(0)
            setSearchQuery(e.target.value)
          }}
        />
      </div>

      <div className="data-table-wrapper">
        <table className="data-table">
          <thead>
            <tr>
              <th style={{ width: '45%' }} onClick={() => handleSort('title')}>
                Paper Title & Details {renderSortIndicator('title')}
              </th>
              <th style={{ width: '10%' }} onClick={() => handleSort('year')}>
                Year {renderSortIndicator('year')}
              </th>
              <th style={{ width: '15%' }}>
                Sources & Overlap
              </th>
              <th style={{ width: '15%' }} onClick={() => handleSort('classification')}>
                Classification {renderSortIndicator('classification')}
              </th>
              <th style={{ width: '15%' }}>
                Action
              </th>
            </tr>
          </thead>
          <tbody>
            {sortedCitations.length === 0 ? (
              <tr>
                <td colSpan={5} className="empty-state">
                  <div className="empty-icon">🔍</div>
                  <p>No citing papers match your current filters.</p>
                </td>
              </tr>
            ) : (
              visibleCitations.map((cit, idx) => {
                const authorsStr = (cit.authors || []).join(', ')
                const isUnresolved = cit.classification === 'UNRESOLVED' || cit.needs_review

                return (
                  <tr key={cit.id || cit.doi || `${cit.title}-${idx}`}>
                    <td>
                      <div className="paper-title">{cit.title || 'Untitled Work'}</div>
                      {authorsStr && <div className="paper-authors">{authorsStr}</div>}
                      {cit.venue && <div className="paper-venue">{cit.venue}</div>}
                      {cit.doi && (
                        <div style={{ marginTop: '0.25rem', fontSize: '0.78rem' }}>
                          <a
                            href={`https://doi.org/${cit.doi}`}
                            target="_blank"
                            rel="noopener noreferrer"
                          >
                            doi:{cit.doi} ↗
                          </a>
                        </div>
                      )}
                    </td>

                    <td>
                      <span style={{ fontWeight: 600 }}>{cit.year || '—'}</span>
                    </td>

                    <td>
                      <div className="source-badges-cell">
                        {(cit.sources || []).map((src) => {
                          const link = (cit.source_links || []).find(l => l.source === src)
                          return link ? (
                            <a
                              key={src}
                              href={link.url}
                              target="_blank"
                              rel="noopener noreferrer"
                              className={`badge badge-source ${src}`}
                              title={`View original on ${src}`}
                            >
                              {src === 'semantic_scholar' ? 'S2' : src} ↗
                            </a>
                          ) : (
                            <span key={src} className={`badge badge-source ${src}`}>
                              {src === 'semantic_scholar' ? 'S2' : src}
                            </span>
                          )
                        })}
                      </div>
                      {cit.duplicate_count > 1 && (
                        <div style={{ fontSize: '0.72rem', color: '#86efac', marginTop: '0.25rem' }}>
                          Merged from {cit.duplicate_count} sources
                        </div>
                      )}
                    </td>

                    <td>
                      <div>
                        {cit.classification === 'SELF' && (
                          <span className="badge badge-self">Self-Citation</span>
                        )}
                        {cit.classification === 'EXTERNAL' && (
                          <span className="badge badge-external">External</span>
                        )}
                        {cit.classification === 'UNRESOLVED' && (
                          <span className="badge badge-unresolved">Unresolved</span>
                        )}
                      </div>
                      <div style={{ fontSize: '0.72rem', color: 'var(--text-muted)', marginTop: '0.25rem' }}>
                        Conf: {(Number(cit.confidence || 0) * 100).toFixed(0)}%
                      </div>
                      {cit.evidence && (
                        <div
                          style={{
                            fontSize: '0.7rem',
                            color: 'var(--text-secondary)',
                            maxWidth: '240px',
                            overflow: 'hidden',
                            textOverflow: 'ellipsis',
                            whiteSpace: 'nowrap',
                            marginTop: '0.15rem'
                          }}
                          title={cit.evidence}
                        >
                          {cit.evidence}
                        </div>
                      )}
                    </td>

                    <td>
                      {isUnresolved && cit.id ? (
                        <div style={{ display: 'flex', gap: '0.35rem', flexWrap: 'wrap' }}>
                          <button
                            type="button"
                            className="btn-claim"
                            style={{ fontSize: '0.75rem', padding: '0.3rem 0.6rem' }}
                            onClick={() => onReviewCitation(cit.id, 'claim')}
                            title="Confirm as Self Citation"
                          >
                            Claim
                          </button>
                          <button
                            type="button"
                            className="btn-reject"
                            style={{ fontSize: '0.75rem', padding: '0.3rem 0.6rem' }}
                            onClick={() => onReviewCitation(cit.id, 'reject')}
                            title="Mark as External Citation"
                          >
                            Reject
                          </button>
                        </div>
                      ) : (
                        <span style={{ fontSize: '0.75rem', color: 'var(--text-muted)' }}>
                          Resolved
                        </span>
                      )}
                    </td>
                  </tr>
                )
              })
            )}
          </tbody>
        </table>
      </div>
      {pageCount > 1 && (
        <div className="table-pagination">
          <button
            type="button"
            className="filter-btn"
            disabled={page === 0}
            onClick={() => setPage((current) => current - 1)}
          >
            Previous
          </button>
          <span>
            Page {page + 1} of {pageCount} ({sortedCitations.length} citations)
          </span>
          <button
            type="button"
            className="filter-btn"
            disabled={page >= pageCount - 1}
            onClick={() => setPage((current) => current + 1)}
          >
            Next
          </button>
        </div>
      )}
    </div>
  )
}
