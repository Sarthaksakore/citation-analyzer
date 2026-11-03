import React, { useState, useEffect } from 'react'
import { api } from './api'
import { Header } from './components/Header'
import { AuthScreen } from './components/AuthScreen'
import { SearchSection } from './components/SearchSection'
import { DashboardOverview } from './components/DashboardOverview'
import { CitationsTable } from './components/CitationsTable'
import { ReviewQueue } from './components/ReviewQueue'
import './index.css'

export default function App() {
  const [activeTab, setActiveTab] = useState('search') // 'search' | 'dashboard' | 'citations' | 'review'
  const [serverStatus, setServerStatus] = useState('connecting')
  const [storedResearchers, setStoredResearchers] = useState([])
  const [candidates, setCandidates] = useState([])
  const [isResolving, setIsResolving] = useState(false)
  const [loadingState, setLoadingState] = useState({ active: false, step: 0, title: '', message: '' })
  const [analysisData, setAnalysisData] = useState(null)
  const [toast, setToast] = useState(null)
  const [authState, setAuthState] = useState({ loading: true, user: null })

  const showToast = (message, type = 'success') => {
    setToast({ message, type })
    setTimeout(() => {
      setToast(null)
    }, 4000)
  }

  // Initial load: check health and stored researchers
  useEffect(() => {
    let frameId
    let running = true
    let pointerX = 0.5
    let pointerY = 0.5
    let targetPointerX = 0.5
    let targetPointerY = 0.5
    let pointerClientX = window.innerWidth / 2
    let pointerClientY = window.innerHeight / 2
    let targetClientX = pointerClientX
    let targetClientY = pointerClientY
    let targetScroll = 0

    const updatePointer = (event) => {
      targetPointerX = event.clientX / window.innerWidth
      targetPointerY = event.clientY / window.innerHeight
      targetClientX = event.clientX
      targetClientY = event.clientY
    }

    const updateAmbientScroll = () => {
      targetScroll = window.scrollY
    }

    window.addEventListener('scroll', updateAmbientScroll, { passive: true })
    window.addEventListener('pointermove', updatePointer, { passive: true })
    updateAmbientScroll()

    const animateAmbient = () => {
      if (!running) return
      pointerX += (targetPointerX - pointerX) * 0.065
      pointerY += (targetPointerY - pointerY) * 0.065
      pointerClientX += (targetClientX - pointerClientX) * 0.1
      pointerClientY += (targetClientY - pointerClientY) * 0.1
      const maxScroll = document.documentElement.scrollHeight - window.innerHeight
      const progress = maxScroll > 0 ? targetScroll / maxScroll : 0
      document.documentElement.style.setProperty('--scroll-progress', progress.toFixed(3))
      document.documentElement.style.setProperty('--scroll-y', `${targetScroll}px`)
      document.documentElement.style.setProperty('--ambient-shift-x', `${(pointerX - 0.5) * 24}px`)
      document.documentElement.style.setProperty('--ambient-shift-y', `${(pointerY - 0.5) * 18}px`)
      document.documentElement.style.setProperty('--cursor-x', pointerX.toFixed(4))
      document.documentElement.style.setProperty('--cursor-y', pointerY.toFixed(4))
      document.documentElement.style.setProperty('--cursor-px', `${pointerClientX}px`)
      document.documentElement.style.setProperty('--cursor-py', `${pointerClientY}px`)
      frameId = window.requestAnimationFrame(animateAmbient)
    }

    frameId = window.requestAnimationFrame(animateAmbient)

    return () => {
      running = false
      window.removeEventListener('scroll', updateAmbientScroll)
      window.removeEventListener('pointermove', updatePointer)
      window.cancelAnimationFrame(frameId)
    }
  }, [])

  useEffect(() => {
    const initApp = async () => {
      try {
        const health = await api.checkHealth()
        setServerStatus(health?.status === 'ok' ? 'connected' : 'disconnected')
      } catch (err) {
        setServerStatus('disconnected')
      }

      try {
        const session = await api.getCurrentUser()
        setAuthState({ loading: false, user: session.user })
      } catch (_) {
        setAuthState({ loading: false, user: null })
        return
      }

      try {
        const res = await api.getResearchers()
        if (res?.researchers) {
          setStoredResearchers(res.researchers)
        }
      } catch (err) {
        console.warn('Could not load stored researchers:', err)
      }
    }

    initApp()
  }, [])

  // Re-fetch stored researchers every time the Search tab becomes active.
  useEffect(() => {
    if (!authState.user || activeTab !== 'search') return
    api.getResearchers()
      .then((res) => { if (res?.researchers) setStoredResearchers(res.researchers) })
      .catch(() => { })
  }, [activeTab, authState.user])

  const handleLogout = async () => {
    try {
      await api.logout()
    } finally {
      setAnalysisData(null)
      setCandidates([])
      setStoredResearchers([])
      setAuthState({ loading: false, user: null })
    }
  }

  if (authState.loading) {
    return <main className="auth-page"><div className="auth-card"><p>Loading secure session...</p></div></main>
  }

  if (!authState.user) {
    return <AuthScreen onAuthenticated={(user) => setAuthState({ loading: false, user })} />
  }

  // Resolve researcher candidates (Layer 1)
  const handleResolve = async (query) => {
    setIsResolving(true)
    setCandidates([])
    try {
      const res = await api.resolveResearcher(query)
      if (res.candidates && res.candidates.length > 0) {
        setCandidates(res.candidates)
        showToast(`Found ${res.candidates.length} matching candidate(s)`)
      } else {
        showToast('No matching researchers found for this query.', 'error')
      }
    } catch (err) {
      showToast(err.message || 'Researcher resolution failed', 'error')
    } finally {
      setIsResolving(false)
    }
  }

  // Run full pipeline (Layers 2-5)
  const handleAnalyze = async (researcherCandidate, { maxPubs = null, maxPerSource = null } = {}) => {
    const authorId = researcherCandidate.openalex_id || researcherCandidate.id
    setLoadingState({
      active: true,
      step: 1,
      title: `Analyzing ${researcherCandidate.display_name || authorId}`,
      message: 'Resolving author profile and publication list...',
    })

    // Simulated step animation while async pipeline runs
    const timer1 = setTimeout(() => {
      setLoadingState((prev) => ({
        ...prev,
        step: 2,
        message: 'Querying OpenAlex and Semantic Scholar citation graphs...',
      }))
    }, 2500)

    const timer2 = setTimeout(() => {
      setLoadingState((prev) => ({
        ...prev,
        step: 3,
        message: 'Normalizing metadata & fuzzy deduplicating citing records...',
      }))
    }, 6000)

    const timer3 = setTimeout(() => {
      setLoadingState((prev) => ({
        ...prev,
        step: 4,
        message: 'Disambiguating co-authors & classifying SELF vs EXTERNAL...',
      }))
    }, 10000)

    try {
      const result = await api.analyzeResearcher(authorId, {
        maxPublications: maxPubs,
        maxResultsPerSource: maxPerSource,
        persist: true,
      })

      setLoadingState((prev) => ({
        ...prev,
        step: 5,
        message: 'Synthesizing statistics and generating HMAC-SHA256 signature...',
      }))

      setTimeout(async () => {
        setAnalysisData(result)
        setCandidates([])  // clear so cached profiles show when user goes back to Search
        setLoadingState({ active: false, step: 0, title: '', message: '' })
        setActiveTab('dashboard')
        showToast(`Analysis completed: ${result.statistics?.deduplicated_count || result.citations?.length} citations processed`)

        // Refresh stored list so new researcher appears immediately
        try {
          const res = await api.getResearchers()
          if (res?.researchers) setStoredResearchers(res.researchers)
        } catch (_) { }
      }, 800)
    } catch (err) {
      clearTimeout(timer1)
      clearTimeout(timer2)
      clearTimeout(timer3)
      setLoadingState({ active: false, step: 0, title: '', message: '' })
      showToast(`Analysis failed: ${err.message}`, 'error')
    }
  }

  // Load cached citations from DB
  const handleLoadCached = async (authorId, researcherObj) => {
    setLoadingState({
      active: true,
      step: 1,
      title: `Loading Stored Citations`,
      message: `Retrieving cached data from local SQLite database for ${researcherObj?.display_name || authorId}...`,
    })

    try {
      const [citationsRes, statsRes, scholarRes] = await Promise.all([
        api.getCachedCitations(authorId),
        api.getStatistics(authorId).catch(() => null),
        api.getCachedGoogleScholar(authorId).catch(() => null),
      ])

      const dedupedCount = citationsRes.citations?.length || 0
      const rawCount = statsRes?.total_raw || (citationsRes.citations || []).reduce((acc, c) => acc + (c.duplicate_count || 1), 0)
      const duplicatesRemoved = Math.max(rawCount - dedupedCount, 0)

      const builtData = {
        researcher: citationsRes.researcher,
        citations: citationsRes.citations || [],
        statistics: statsRes || {
          total_raw: rawCount,
          deduplicated_count: dedupedCount,
          self_citation_count: (citationsRes.citations || []).filter(c => c.classification === 'SELF').length,
          external_citation_count: (citationsRes.citations || []).filter(c => c.classification === 'EXTERNAL').length,
          unresolved_count: (citationsRes.citations || []).filter(c => c.classification === 'UNRESOLVED' || c.needs_review).length,
        },
        dedup_summary: {
          duplicates_removed: duplicatesRemoved,
          raw_count: rawCount,
          unique_count: dedupedCount,
        },
        google_scholar: scholarRes?.google_scholar || {
          enabled: false,
          source: 'google_scholar',
          reason: 'Scholar data unavailable for cached profile',
        },
      }

      setAnalysisData(builtData)
      setLoadingState({ active: false, step: 0, title: '', message: '' })
      setActiveTab('dashboard')
      showToast(`Loaded ${citationsRes.citations?.length || 0} cached citations`)
    } catch (err) {
      setLoadingState({ active: false, step: 0, title: '', message: '' })
      showToast(`Failed to load cached citations: ${err.message}`, 'error')
    }
  }

  // Human review decision (Claim/Reject)
  const handleReviewCitation = async (citationId, decision, reviewerNote = '') => {
    if (!citationId) {
      showToast('This citation has no stored ID. Re-analyze the profile before reviewing it.', 'error')
      return
    }
    try {
      const res = await api.reviewCitation(citationId, decision, reviewerNote)
      showToast(res.message || `Citation ${decision === 'claim' ? 'claimed as SELF' : 'rejected as EXTERNAL'}`)

      // Update state locally so UI updates instantly
      setAnalysisData((prev) => {
        if (!prev) return prev
        const newClassification = decision === 'claim' ? 'SELF' : 'EXTERNAL'

        const updatedCitations = prev.citations.map((c) => {
          if (c.id === citationId) {
            return {
              ...c,
              classification: newClassification,
              confidence: 1.0,
              needs_review: false,
              evidence: res.evidence || c.evidence,
            }
          }
          return c
        })

        // Update statistics counters
        const prevStats = prev.statistics || {}
        const newStats = {
          ...prevStats,
          unresolved_count: Math.max(0, (prevStats.unresolved_count || 1) - 1),
          self_citation_count: decision === 'claim' ? (prevStats.self_citation_count || 0) + 1 : (prevStats.self_citation_count || 0),
          external_citation_count: decision === 'reject' ? (prevStats.external_citation_count || 0) + 1 : (prevStats.external_citation_count || 0),
        }

        return {
          ...prev,
          citations: updatedCitations,
          statistics: newStats,
        }
      })
    } catch (err) {
      showToast(`Review action failed: ${err.message}`, 'error')
    }
  }

  // Delete single cached researcher
  const handleDeleteCachedResearcher = async (authorId) => {
    try {
      await api.deleteResearcher(authorId)
      showToast('Cached profile removed successfully')
      const res = await api.getResearchers()
      setStoredResearchers(res?.researchers || [])
    } catch (err) {
      showToast(`Failed to delete profile: ${err.message}`, 'error')
    }
  }

  // Clear all cached researchers
  const handleClearAllCached = async () => {
    try {
      await api.clearAllResearchers()
      showToast('All cached profiles removed')
      setStoredResearchers([])
    } catch (err) {
      showToast(`Failed to clear cache: ${err.message}`, 'error')
    }
  }

  const unresolvedCount = analysisData?.citations
    ? analysisData.citations.filter((c) => c.classification === 'UNRESOLVED' || c.needs_review).length
    : 0

  return (
    <div className="app-container">
      <div className="ambient-background" aria-hidden="true">
        <span className="ambient-light ambient-light-left" />
        <span className="ambient-light ambient-light-right" />
        <span className="ambient-light ambient-light-top" />
        <span className="ambient-light ambient-light-bottom" />
        <span className="cursor-light" />
      </div>
      <Header
        activeTab={activeTab}
        setActiveTab={setActiveTab}
        currentResearcher={analysisData?.researcher}
        unresolvedCount={unresolvedCount}
        serverStatus={serverStatus}
        user={authState.user}
        onLogout={handleLogout}
      />

      <main className="main-content">
        {activeTab === 'search' && (
          <SearchSection
            onResolve={handleResolve}
            onAnalyze={handleAnalyze}
            onLoadCached={handleLoadCached}
            onDeleteCached={handleDeleteCachedResearcher}
            onClearAllCached={handleClearAllCached}
            candidates={candidates}
            setCandidates={setCandidates}
            storedResearchers={storedResearchers}
            loadingState={loadingState}
            isResolving={isResolving}
          />
        )}

        {activeTab === 'dashboard' && analysisData && (
          <DashboardOverview
            analysisData={analysisData}
            onNavigateTab={setActiveTab}
          />
        )}

        {activeTab === 'citations' && analysisData && (
          <CitationsTable
            citations={analysisData.citations}
            onReviewCitation={handleReviewCitation}
          />
        )}

        {activeTab === 'review' && analysisData && (
          <ReviewQueue
            citations={analysisData.citations}
            onReviewCitation={handleReviewCitation}
            onNavigateTab={setActiveTab}
          />
        )}
      </main>

      {/* Floating Toast Notification */}
      {toast && (
        <div className={`toast ${toast.type}`}>
          <span>{toast.type === 'success' ? '✓' : '⚠'}</span>
          <span>{toast.message}</span>
        </div>
      )}
    </div>
  )
}
