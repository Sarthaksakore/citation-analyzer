import React, { useState } from 'react'

export function ReviewQueue({
  citations = [],
  onReviewCitation,
  onNavigateTab,
}) {
  const [notes, setNotes] = useState({})
  const [submittingId, setSubmittingId] = useState(null)

  // Filter down to unresolved or needs_review items
  const queueItems = citations.filter(
    (c) => c.classification === 'UNRESOLVED' || c.needs_review
  )

  const handleNoteChange = (id, text) => {
    setNotes((prev) => ({ ...prev, [id]: text }))
  }

  const handleDecision = async (id, decision) => {
    if (!id) return
    setSubmittingId(id)
    try {
      const note = notes[id] || ''
      await onReviewCitation(id, decision, note)
      // Clean up note after submission
      setNotes((prev) => {
        const next = { ...prev }
        delete next[id]
        return next
      })
    } finally {
      setSubmittingId(null)
    }
  }

  return (
    <div className="review-queue-container">
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '1.5rem' }}>
        <div>
          <h2 style={{ fontSize: '1.5rem', fontWeight: 800 }}>
            Human-in-the-Loop Review Queue
          </h2>
          <p style={{ color: 'var(--text-secondary)', fontSize: '0.875rem' }}>
            Confirm whether each citing paper belongs to the researcher. Your decision is saved to the review audit trail.
          </p>
        </div>
        <div className="badge badge-unresolved" style={{ fontSize: '0.85rem', padding: '0.35rem 0.75rem' }}>
          {queueItems.length} Citations Pending
        </div>
      </div>

      {queueItems.length === 0 ? (
        <div className="empty-state" style={{ background: 'var(--bg-card)', borderRadius: 'var(--radius-lg)', border: '1px solid var(--border-subtle)' }}>
          <div className="empty-icon">🎉</div>
          <h3 style={{ fontSize: '1.2rem', color: '#fff', marginBottom: '0.5rem' }}>
            Queue is Clear!
          </h3>
          <p style={{ maxWidth: '450px', margin: '0 auto 1.5rem auto' }}>
            All citations for this researcher have been classified with full confidence or previously resolved.
          </p>
          <button
            type="button"
            className="btn-primary"
            onClick={() => onNavigateTab('dashboard')}
          >
            Return to Dashboard
          </button>
        </div>
      ) : (
        queueItems.map((cit) => {
          const authorsStr = (cit.authors || []).join(', ')
          const isSubmitting = submittingId === cit.id
          const sourceCount = (cit.sources || []).length

          return (
            <div key={cit.id || cit.doi || `${cit.title}-${cit.year || ''}`} className="review-card">
              <div className="review-card-header">
                <div>
                  <div className="paper-title" style={{ fontSize: '1.05rem' }}>
                    {cit.title || 'Untitled Work'}
                  </div>
                  {authorsStr && (
                    <div className="paper-authors" style={{ color: '#cbd5e1' }}>
                      Authors: <strong>{authorsStr}</strong>
                    </div>
                  )}
                  <div style={{ display: 'flex', gap: '0.75rem', marginTop: '0.35rem', fontSize: '0.78rem', color: 'var(--text-muted)' }}>
                    {cit.year && <span>Published: <strong>{cit.year}</strong></span>}
                    {cit.venue && <span>Venue: {cit.venue}</span>}
                    {sourceCount > 0 && <span>Sources: <strong>{sourceCount}</strong></span>}
                    {cit.doi && (
                      <a
                        href={`https://doi.org/${cit.doi}`}
                        target="_blank"
                        rel="noopener noreferrer"
                      >
                        doi:{cit.doi} ↗
                      </a>
                    )}
                  </div>
                </div>

                <div style={{ textAlign: 'right' }}>
                  <span className="badge badge-unresolved">
                    {cit.classification || 'UNRESOLVED'}
                  </span>
                  <div style={{ fontSize: '0.75rem', color: 'var(--text-muted)', marginTop: '0.35rem' }}>
                    Confidence: {(Number(cit.confidence || 0) * 100).toFixed(0)}%
                  </div>
                </div>
              </div>

              {/* Evidence box */}
              {cit.evidence && (
                <div className="evidence-box">
                  <strong>Why this needs review:</strong> {cit.evidence}
                </div>
              )}
              {!cit.id && (
                <div className="evidence-box" style={{ color: '#fcd34d' }}>
                  This record cannot be reviewed yet because it has no stored citation ID. Re-analyze the profile to create a reviewable record.
                </div>
              )}

              {/* Action and Note Bar */}
              <div className="review-actions-bar">
                <input
                  type="text"
                  className="review-note-input"
                  placeholder="Optional review audit note (e.g. 'Verified via lab website CV')..."
                  value={notes[cit.id] || ''}
                  onChange={(e) => handleNoteChange(cit.id, e.target.value)}
                  disabled={isSubmitting}
                />

                <div className="review-buttons">
                  <button
                    type="button"
                    className="btn-claim"
                    disabled={isSubmitting || !cit.id}
                    onClick={() => handleDecision(cit.id, 'claim')}
                  >
                    <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5">
                      <polyline points="20 6 9 17 4 12" />
                    </svg>
                    Claim as Self
                  </button>

                  <button
                    type="button"
                    className="btn-reject"
                    disabled={isSubmitting || !cit.id}
                    onClick={() => handleDecision(cit.id, 'reject')}
                  >
                    <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5">
                      <line x1="18" y1="6" x2="6" y2="18" />
                      <line x1="6" y1="6" x2="18" y2="18" />
                    </svg>
                    Reject as External
                  </button>
                </div>
              </div>
            </div>
          )
        })
      )}
    </div>
  )
}
