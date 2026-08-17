import { useState } from 'react'
import { useAuth } from './AuthContext.jsx'
import AuthModal from './AuthModal.jsx'
import './LandingPage.css'

const DEMO_LINES = {
  hindi: [
    'नमस्ते! आज हम बात करेंगे स्टेप-अप SIP के बारे में।',
    'अगर आप हर साल 20% अपनी SIP बढ़ाते हैं...',
    '...तो 20 साल में आपके पास 1.3 करोड़ रुपये हो सकते हैं।',
  ],
  english: [
    "Hey! Today we're talking about step-up SIPs.",
    "If you increase your SIP by 20% every year...",
    '...you could have 1.3 crore rupees in 20 years.',
  ],
  hinglish: [
    'Namaste! Aaj hum baat karenge step-up SIP ke baare mein.',
    'Agar aap har saal apni SIP 20% badhate hain...',
    '...toh 20 saal mein aapke paas 1.3 crore rupee ho sakte hain.',
  ],
}

const FEATURES = [
  {
    title: 'Works without captions',
    body: 'No subtitles on the post? Doesn\u2019t matter — audio is transcribed directly, so you get a full script even from raw, uncaptioned Reels.',
  },
  {
    title: 'Real Hinglish, not literal translation',
    body: 'Most tools give you stiff, formal Hindi. Scriptly reads the way your audience actually speaks — code-switched, natural, unforced.',
  },
  {
    title: 'Built for short-form',
    body: 'Optimized for Reels and Shorts under 15 minutes — the format Indian creators actually post in, processed in minutes not hours.',
  },
]

const STEPS = [
  {
    n: '01',
    title: 'Paste the link',
    body: 'Drop in any public YouTube video or Instagram Reel URL.',
  },
  {
    n: '02',
    title: 'AI transcribes & translates',
    body: 'Audio is extracted and transcribed, then converted into Hindi, English, and Hinglish.',
  },
  {
    n: '03',
    title: 'Copy or download',
    body: 'Grab the script in whichever language you need, ready to repurpose.',
  },
]

const FAQS = [
  {
    q: 'Does it work on private Instagram accounts?',
    a: 'Not by default — public Reels and videos only. Private-account support requires additional authentication and isn\u2019t part of the standard flow.',
  },
  {
    q: 'How accurate is the Hinglish output?',
    a: 'It\u2019s generated to preserve every detail of the original — no summarizing, adding, or dropping content — while reading naturally in Roman-script Hinglish, the way it was actually spoken.',
  },
  {
    q: 'Is there a length limit?',
    a: 'Yes — videos longer than 15 minutes are rejected upfront to keep turnaround fast and predictable.',
  },
  {
    q: 'Is it free to use?',
    a: 'Pricing details will land here once the product is live — for now, this is in active testing.',
  },
]

function DemoStrip() {
  const [tab, setTab] = useState('hinglish')
  return (
    <div className="demo-strip">
      <div className="demo-tabs">
        {['hindi', 'english', 'hinglish'].map((t) => (
          <button
            key={t}
            className={`demo-tab ${tab === t ? 'active' : ''}`}
            onClick={() => setTab(t)}
          >
            {t.toUpperCase()}
          </button>
        ))}
      </div>
      <div className="demo-panel">
        {DEMO_LINES[tab].map((line, i) => (
          <div className="demo-line" key={i} style={{ animationDelay: `${i * 0.12}s` }}>
            <span className="demo-line-no">{String(i + 1).padStart(2, '0')}</span>
            <span className={tab === 'hindi' ? 'font-deva' : ''}>{line}</span>
          </div>
        ))}
      </div>
    </div>
  )
}

function FaqItem({ q, a }) {
  const [open, setOpen] = useState(false)
  return (
    <div className="faq-item">
      <button className="faq-q" onClick={() => setOpen(!open)} aria-expanded={open}>
        <span>{q}</span>
        <span className="faq-toggle">{open ? '−' : '+'}</span>
      </button>
      {open && <p className="faq-a">{a}</p>}
    </div>
  )
}

export default function LandingPage({ onGetStarted }) {
  const [url, setUrl] = useState('')
  const [showAuth, setShowAuth] = useState(false)
  const [authMode, setAuthMode] = useState('login')
  const { user, logout } = useAuth()

  const submit = (e) => {
    e.preventDefault()
    onGetStarted(url.trim())
  }

  return (
    <div className="landing">
      {/* Hero */}
      <section className="hero">
        <div className="nav-bar">
          <div className="brand">
            <span className="brand-mark">▍▍▍</span>
            <span className="brand-name">SCRIPTLY</span>
          </div>
          <div className="nav-auth">
            {user ? (
              <>
                <span className="nav-email">{user.email}</span>
                <button className="nav-link" onClick={logout}>
                  Log out
                </button>
              </>
            ) : (
              <>
                <button className="nav-link" onClick={() => { setAuthMode('login'); setShowAuth(true) }}>
                  Login
                </button>
                <button className="nav-signup" onClick={() => { setAuthMode('signup'); setShowAuth(true) }}>
                  Sign Up
                </button>
              </>
            )}
          </div>
        </div>
        <h1>
          Turn any Reel into a script —<br />in Hindi, English, and Hinglish.
        </h1>
        <p className="hero-sub">
          Paste a link, get the full spoken transcript back in seconds — no captions needed.
        </p>
        <form className="hero-form" onSubmit={submit}>
          <input
            type="url"
            required
            placeholder="Paste a YouTube or Instagram Reel link…"
            value={url}
            onChange={(e) => setUrl(e.target.value)}
          />
          <button type="submit">Get Script</button>
        </form>
        <p className="trust-line">Works with YouTube and Instagram Reels — no login needed</p>
      </section>

      {/* Product demo strip */}
      <section className="section">
        <DemoStrip />
      </section>

      {/* Features */}
      <section className="section features">
        {FEATURES.map((f) => (
          <div className="feature-card" key={f.title}>
            <div className="feature-mark">●</div>
            <h3>{f.title}</h3>
            <p>{f.body}</p>
          </div>
        ))}
      </section>

      {/* How it works */}
      <section className="section how-it-works">
        <h2>How it works</h2>
        <div className="steps">
          {STEPS.map((s) => (
            <div className="step" key={s.n}>
              <span className="step-n">{s.n}</span>
              <h3>{s.title}</h3>
              <p>{s.body}</p>
            </div>
          ))}
        </div>
      </section>

      {/* FAQ */}
      <section className="section faq">
        <h2>Frequently asked questions</h2>
        <div className="faq-list">
          {FAQS.map((f) => (
            <FaqItem key={f.q} {...f} />
          ))}
        </div>
      </section>

      {/* Closing CTA */}
      <section className="section closing-cta">
        <h2>Ready to turn a Reel into a script?</h2>
        <form className="hero-form" onSubmit={submit}>
          <input
            type="url"
            required
            placeholder="Paste a YouTube or Instagram Reel link…"
            value={url}
            onChange={(e) => setUrl(e.target.value)}
          />
          <button type="submit">Get Script</button>
        </form>
      </section>

      {showAuth && <AuthModal initialMode={authMode} onClose={() => setShowAuth(false)} />}
    </div>
  )
}