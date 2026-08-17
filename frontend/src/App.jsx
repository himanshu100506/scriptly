import { useState, useRef, useEffect, useCallback } from 'react'
import LandingPage from './LandingPage.jsx'
import './App.css'

const API_BASE = import.meta.env.VITE_API_BASE || 'http://localhost:8000'

const TABS = [
  { key: 'original_transcript', label: 'ORIGINAL', mark: '●', font: 'deva' },
  { key: 'hindi', label: 'HINDI', mark: '●', font: 'deva' },
  { key: 'english', label: 'ENGLISH', mark: '●', font: 'sans' },
  { key: 'hinglish', label: 'HINGLISH', mark: '●', font: 'sans' },
]

const STAGE_LABELS = {
  pending: 'queued — waiting for a worker',
  started: 'processing — downloading, transcribing, translating',
}

function WaveformLoader() {
  return (
    <div className="waveform" aria-hidden="true">
      {Array.from({ length: 24 }).map((_, i) => (
        <span key={i} style={{ animationDelay: `${(i % 8) * 0.08}s` }} />
      ))}
    </div>
  )
}

function TranscriptPanel({ text, font }) {
  const lines = text.split(/(?<=[.!?।])\s+/).filter(Boolean)
  return (
    <div className={`transcript-panel font-${font}`}>
      {lines.map((line, i) => (
        <div className="transcript-line" key={i}>
          <span className="line-no">{String(i + 1).padStart(2, '0')}</span>
          <span className="line-text">{line}</span>
        </div>
      ))}
    </div>
  )
}

export default function App() {
  const [showLanding, setShowLanding] = useState(true)
  const [url, setUrl] = useState('')
  const [jobId, setJobId] = useState(null)
  const [status, setStatus] = useState('idle') // idle | pending | started | success | failure
  const [result, setResult] = useState(null)
  const [error, setError] = useState(null)
  const [activeTab, setActiveTab] = useState('hinglish')
  const [copied, setCopied] = useState(false)
  const pollRef = useRef(null)

  const reset = () => {
    setJobId(null)
    setStatus('idle')
    setResult(null)
    setError(null)
    setCopied(false)
    if (pollRef.current) clearInterval(pollRef.current)
  }

  const submit = async (e) => {
    e.preventDefault()
    if (!url.trim()) return
    await submitJob(url.trim())
  }

  const submitJob = async (targetUrl) => {
    reset()
    setStatus('pending')
    try {
      const res = await fetch(`${API_BASE}/api/jobs`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ url: targetUrl }),
      })
      if (!res.ok) {
        const body = await res.json().catch(() => ({}))
        throw new Error(body.detail?.[0]?.msg || `Request failed (${res.status})`)
      }
      const data = await res.json()
      setJobId(data.job_id)
    } catch (err) {
      setStatus('failure')
      setError(err.message || 'Could not reach the backend. Is it running?')
    }
  }

  const handleGetStarted = (targetUrl) => {
    if (!targetUrl) return
    setUrl(targetUrl)
    setShowLanding(false)
    submitJob(targetUrl)
  }

  const poll = useCallback(async (id) => {
    try {
      const res = await fetch(`${API_BASE}/api/jobs/${id}`)
      if (!res.ok) throw new Error(`Status check failed (${res.status})`)
      const data = await res.json()
      setStatus(data.status)
      if (data.status === 'success') {
        setResult(data.result)
        clearInterval(pollRef.current)
      } else if (data.status === 'failure') {
        setError(cleanError(data.error))
        clearInterval(pollRef.current)
      }
    } catch (err) {
      setStatus('failure')
      setError(err.message || 'Lost connection to the backend.')
      clearInterval(pollRef.current)
    }
  }, [])

  useEffect(() => {
    if (!jobId) return
    poll(jobId)
    pollRef.current = setInterval(() => poll(jobId), 3000)
    return () => clearInterval(pollRef.current)
  }, [jobId, poll])

  const copyActive = async () => {
    if (!result) return
    await navigator.clipboard.writeText(result[activeTab])
    setCopied(true)
    setTimeout(() => setCopied(false), 1500)
  }

  const downloadActive = () => {
    if (!result) return
    const blob = new Blob([result[activeTab]], { type: 'text/plain;charset=utf-8' })
    const link = document.createElement('a')
    link.href = URL.createObjectURL(blob)
    link.download = `${activeTab}-script.txt`
    link.click()
  }

  const isBusy = status === 'pending' || status === 'started'
  const activeMeta = TABS.find((t) => t.key === activeTab)

  if (showLanding) {
    return <LandingPage onGetStarted={handleGetStarted} />
  }

  return (
    <div className="app">
      <header className="app-header">
        <div className="brand">
          <button className="brand-link" onClick={() => setShowLanding(true)}>
            <span className="brand-mark">▍▍▍</span>
            <span className="brand-name">SCRIPTLY</span>
          </button>
        </div>
        <p className="tagline">
          paste a reel or video — get the spoken script back in hindi, english &amp; hinglish
        </p>
      </header>

      <form className="url-form" onSubmit={submit}>
        <input
          type="url"
          required
          placeholder="https://www.instagram.com/reel/... or youtube.com/watch?v=..."
          value={url}
          onChange={(e) => setUrl(e.target.value)}
          disabled={isBusy}
        />
        <button type="submit" disabled={isBusy || !url.trim()}>
          {isBusy ? 'working…' : 'get script'}
        </button>
      </form>

      {isBusy && (
        <div className="status-block">
          <WaveformLoader />
          <p className="status-label">{STAGE_LABELS[status]}</p>
          <p className="status-sub">this can take a few minutes on longer clips</p>
        </div>
      )}

      {status === 'failure' && (
        <div className="error-block">
          <p className="error-title">couldn't process that video</p>
          <p className="error-detail">{error}</p>
        </div>
      )}

      {result && (
        <div className="result">
          <div className="meta-card">
            <div className="meta-title">{result.metadata.title || 'Untitled'}</div>
            <div className="meta-row">
              <span>{result.metadata.uploader || 'unknown uploader'}</span>
              {result.metadata.duration ? (
                <span>{formatDuration(result.metadata.duration)}</span>
              ) : null}
            </div>
          </div>

          <div className="tabs" role="tablist">
            {TABS.map((t) => (
              <button
                key={t.key}
                role="tab"
                aria-selected={activeTab === t.key}
                className={`tab ${activeTab === t.key ? 'active' : ''}`}
                onClick={() => setActiveTab(t.key)}
              >
                {t.label}
              </button>
            ))}
          </div>

          <TranscriptPanel text={result[activeTab]} font={activeMeta.font} />

          <div className="actions">
            <button onClick={copyActive}>{copied ? 'copied ✓' : 'copy'}</button>
            <button onClick={downloadActive}>download .txt</button>
          </div>
        </div>
      )}
    </div>
  )
}

function formatDuration(sec) {
  if (!sec) return null
  const m = Math.floor(sec / 60)
  const s = sec % 60
  return `${m}:${String(s).padStart(2, '0')}`
}

function cleanError(raw) {
  if (!raw) return 'Unknown error.'
  if (raw.includes('VIDEO_TOO_LONG')) return 'This video is longer than the 15-minute cap.'
  if (raw.includes('DOWNLOAD_FAILED'))
    return "Couldn't download this video — it may be private, deleted, or age-restricted."
  if (raw.includes('TIMEOUT')) return 'Processing took too long and was stopped.'
  return raw
}