// A call transcript as chat lines, plus the recording player when one exists.
export default function Transcript({ messages, audioSrc, loading }) {
  return (
    <div>
      {audioSrc && (
        <div className="card" style={{ marginBottom: 14, padding: 12 }}>
          <label>Recording</label>
          <audio controls preload="none" src={audioSrc} style={{ width: '100%' }} />
        </div>
      )}
      <div className="stack" style={{ gap: 8 }}>
        {loading ? <div className="muted">Loading transcript…</div>
          : Array.isArray(messages) && messages.length ? messages.map((m, i) => (
            <div key={i} style={{ fontSize: '0.84rem', lineHeight: 1.5 }}>
              <b style={{ color: m.role === 'user' ? 'var(--blue)' : 'var(--green)' }}>
                {m.role === 'user' ? 'Caller' : 'Agent'}:
              </b>{' '}
              {m.text}
            </div>
          )) : <div className="muted">No transcript recorded.</div>}
      </div>
    </div>
  )
}
