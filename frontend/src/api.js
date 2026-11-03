/**
 * api.js — Client interface for Citation Analyzer FastAPI backend.
 * Supports /api proxy or direct http://localhost:8000 fallback.
 */

const API_BASE = import.meta.env.VITE_API_BASE_URL || '/api'
const DIRECT_BACKEND = 'http://127.0.0.1:8000'

async function request(endpoint, options = {}) {
  const url = `${API_BASE}${endpoint}`
  const directUrl = `${DIRECT_BACKEND}${endpoint}`

  const config = {
    headers: {
      'Content-Type': 'application/json',
      ...options.headers,
    },
    ...options,
  }

  try {
    const res = await fetch(url, config)
    if (!res.ok) {
      const errData = await res.json().catch(() => ({ detail: res.statusText }))
      throw new Error(errData.detail || `Request failed with status ${res.status}`)
    }
    return await res.json()
  } catch (err) {
    // If proxy failed or relative path rejected, try direct backend fallback
    if (API_BASE !== DIRECT_BACKEND && err.message?.includes('Failed to fetch')) {
      try {
        const directRes = await fetch(directUrl, config)
        if (!directRes.ok) {
          const errData = await directRes.json().catch(() => ({ detail: directRes.statusText }))
          throw new Error(errData.detail || `Request failed with status ${directRes.status}`)
        }
        return await directRes.json()
      } catch (fallbackErr) {
        throw new Error(`Backend connection error: ${fallbackErr.message || fallbackErr}`)
      }
    }
    throw err
  }
}

export const api = {
  // Meta
  checkHealth: () => request('/health'),

  // Stored researchers
  getResearchers: () => request('/researchers'),
  deleteResearcher: (researcherId) =>
    request(`/researcher/${encodeURIComponent(researcherId)}`, { method: 'DELETE' }),
  clearAllResearchers: () =>
    request('/researchers', { method: 'DELETE' }),

  // Layer 1: Resolve researcher candidates
  resolveResearcher: (query) =>
    request('/researcher/resolve', {
      method: 'POST',
      body: JSON.stringify({ query }),
    }),

  // Layers 2-5: Full analysis pipeline
  analyzeResearcher: (researcherId, { maxPublications = null, maxResultsPerSource = null, persist = true } = {}) =>
    request(`/researcher/${encodeURIComponent(researcherId)}/analyze`, {
      method: 'POST',
      body: JSON.stringify({
        max_publications: maxPublications,
        max_results_per_source: maxResultsPerSource,
        persist,
      }),
    }),

  // Stored / cached citations
  getCachedCitations: (researcherId, classification = null) => {
    const query = classification ? `?classification=${encodeURIComponent(classification)}` : ''
    return request(`/researcher/${encodeURIComponent(researcherId)}/citations${query}`)
  },
  getCachedGoogleScholar: (researcherId) =>
    request(`/researcher/${encodeURIComponent(researcherId)}/google-scholar`),

  // Layer 6: Human review Claim / Reject
  reviewCitation: (citationId, decision, reviewerNote = '') =>
    request(`/citations/${citationId}/review`, {
      method: 'POST',
      body: JSON.stringify({
        decision,
        reviewer_note: reviewerNote || undefined,
      }),
    }),

  // Layer 5: Stored statistics
  getStatistics: (researcherId) =>
    request(`/statistics/${encodeURIComponent(researcherId)}`),

  // Security: Integrity validation
  verifyIntegrity: (summary) =>
    request('/verify-integrity', {
      method: 'POST',
      body: JSON.stringify(summary),
    }),
}
