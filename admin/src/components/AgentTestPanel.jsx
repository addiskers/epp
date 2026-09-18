import { useEffect, useRef, useState } from 'react'
import { api } from '../api.js'

// Test the agent two ways: read the exact prompt it would be given, or actually hear it.
// The browser-mic path goes through a short-lived token — /ws is unauthenticated, so it
// must never take an agent id straight from the query string.
export default function AgentTestPanel({ agent }) {
  const [preview, setPreview] = useState(null)
  const [err, setErr] = useState('')
  const [busy, setBusy] = useState(false)
  const [phone, setPhone] = useState('')
  const [callMsg, setCallMsg] = useState('')

  async function runPreview() {
    setErr(''); setCallMsg(''); setBusy(true)
    try { setPreview(await api.post(`/agents/${agent.id}/preview`, {})) }
    catch (e) { setErr(e.message); setPreview(null) } finally { setBusy(false) }
  }

  async function testCall() {
    setErr(''); setCallMsg(''); setBusy(true)
    try {
      const r = await api.post(`/agents/${agent.id}/test-call`, { phone })
      setCallMsg(`Calling ${r.to || phone}… (call ${r.call_uuid || 'placed'})`)
    } catch (e) { setErr(e.message) } finally { setBusy(false) }
  }

  return (
    <div className="panel">
      <div className="panel-head">
        <div>
          <h3>Test the agent</h3>
          <div className="muted" style={{ fontSize: '0.8rem', marginTop: 3 }}>
            Read the exact script it is given, talk to it from this browser, or have it ring your phone.
            Test calls create real tickets — mark them closed afterwards.
          </div>
        </div>
      </div>

      <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
        <button className="btn" disabled={busy} onClick={runPreview}>{busy ? 'Working…' : 'Show the script'}</button>
        <MicTest agent={agent} disabled={busy} onError={setErr} />
      </div>
      {err && <div className="err" style={{ marginTop: 12 }}>{err}</div>}

      <div style={{ borderTop: '1px solid var(--border)', marginTop: 18, paddingTop: 16 }}>
        <label>Or ring a phone</label>
        <div style={{ display: 'flex', gap: 8 }}>
          <input value={phone} onChange={(e) => setPhone(e.target.value)} placeholder="9876543210 or +9198…" style={{ flex: 1 }} />
          <button className="btn ghost" disabled={busy || !phone} onClick={testCall}>Call me</button>
        </div>
        <div className="muted" style={{ fontSize: '0.76rem', marginTop: 5 }}>
          Places a real outbound call through Plivo and bills like any other.
        </div>
        {callMsg && <div className="pill green" style={{ marginTop: 10 }}>{callMsg}</div>}
      </div>

      {preview && <PreviewOutput preview={preview} />}
    </div>
  )
}

function PreviewOutput({ preview }) {
  const create = (preview.tools || []).find((t) => t.name === 'create_ticket')
  const cats = create?.parameters?.properties?.category?.enum || []
  const langs = create?.parameters?.properties?.language?.enum || []
  return (
    <div style={{ marginTop: 18 }}>
      {!!preview.missing?.length && (
        <div style={{ background: 'var(--amber-soft)', border: '1px solid var(--amber)',
                      borderRadius: 'var(--radius-sm)', padding: '10px 12px', marginBottom: 12 }}>
          <div style={{ fontWeight: 600, fontSize: '0.85rem', color: 'var(--amber)' }}>
            No data for {preview.missing.length} placeholder{preview.missing.length === 1 ? '' : 's'}
          </div>
          <div className="muted" style={{ fontSize: '0.79rem', marginTop: 3 }}>
            {preview.missing.join(', ')} — those sentences are dropped rather than spoken with a gap.
          </div>
        </div>
      )}
      <div className="label-caps">First thing it says</div>
      <pre style={preStyle}>{preview.trigger}</pre>
      <div className="label-caps" style={{ margin: '14px 0 6px' }}>Everything it knows</div>
      <pre style={{ ...preStyle, maxHeight: 460 }}>{preview.system_instruction}</pre>
      <div className="muted" style={{ fontSize: '0.78rem', marginTop: 10 }}>
        Tools: {(preview.tools || []).map((t) => t.name).join(', ')} · {cats.length} categories · languages: {langs.join(', ')}
      </div>
    </div>
  )
}

const preStyle = {
  background: 'var(--bg-2)', border: '1px solid var(--border)', borderRadius: 'var(--radius-sm)',
  padding: 12, fontFamily: 'var(--mono)', fontSize: '0.78rem', lineHeight: 1.55,
  whiteSpace: 'pre-wrap', overflow: 'auto', maxHeight: 240, margin: 0, color: 'var(--text-2)',
}

// Float32 at the browser's rate -> 16kHz PCM16, the format the Live session expects.
function toPCM16(float32, fromRate, toRate) {
  let samples = float32
  if (fromRate !== toRate) {
    const ratio = fromRate / toRate
    const out = new Float32Array(Math.round(float32.length / ratio))
    for (let i = 0; i < out.length; i++) {
      const start = Math.floor(i * ratio)
      const end = Math.min(Math.floor((i + 1) * ratio), float32.length)
      let sum = 0
      for (let j = start; j < end; j++) sum += float32[j]
      out[i] = end > start ? sum / (end - start) : 0
    }
    samples = out
  }
  const pcm = new Int16Array(samples.length)
  for (let i = 0; i < samples.length; i++) {
    const s = Math.max(-1, Math.min(1, samples[i]))
    pcm[i] = s < 0 ? s * 0x8000 : s * 0x7fff
  }
  return pcm.buffer
}

// Browser-mic test: mint a short-lived token, open /ws with it, stream the mic.
function MicTest({ agent, disabled, onError }) {
  const [state, setState] = useState('idle')     // idle | connecting | live
  const [lines, setLines] = useState([])
  const ref = useRef({})

  useEffect(() => () => stop(), [])              // always hang up on unmount

  function stop() {
    const r = ref.current
    try { r.ws?.close() } catch {}
    try { r.stream?.getTracks().forEach((t) => t.stop()) } catch {}
    try { r.ctx?.close() } catch {}
    ref.current = {}
    setState('idle')
  }

  async function start() {
    setLines([]); onError('')
    setState('connecting')
    try {
      const { token } = await api.post(`/agents/${agent.id}/test-token`, {})
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true })
      const ctx = new AudioContext({ sampleRate: 16000 })
      await ctx.audioWorklet.addModule('/static/pcm-processor.js')
      const proto = location.protocol === 'https:' ? 'wss' : 'ws'
      const ws = new WebSocket(`${proto}://${location.host}/ws?test_token=${encodeURIComponent(token)}`)
      ws.binaryType = 'arraybuffer'
      ref.current = { ws, stream, ctx }

      ws.onopen = () => {
        setState('live')
        const src = ctx.createMediaStreamSource(stream)
        const node = new AudioWorkletNode(ctx, 'pcm-processor')
        node.port.onmessage = (e) => {
          if (ws.readyState !== 1) return
          ws.send(toPCM16(e.data, ctx.sampleRate, 16000))
        }
        src.connect(node)
        const mute = ctx.createGain()
        mute.gain.value = 0
        node.connect(mute); mute.connect(ctx.destination)
        ref.current.node = node
      }
      let playAt = 0
      ws.onmessage = (e) => {
        if (typeof e.data === 'string') {
          try {
            const msg = JSON.parse(e.data)
            if (msg.type === 'gemini' && msg.text) setLines((l) => [...l, ['agent', msg.text]])
            if (msg.type === 'user' && msg.text) setLines((l) => [...l, ['you', msg.text]])
            if (msg.type === 'tool_call') setLines((l) => [...l, ['tool', `${msg.name} → ${JSON.stringify(msg.result?.ticket_id || msg.result?.status || msg.result?.ok)}`]])
          } catch {}
          return
        }
        const pcm = new Int16Array(e.data)
        const buf = ctx.createBuffer(1, pcm.length, 24000)
        const ch = buf.getChannelData(0)
        for (let i = 0; i < pcm.length; i++) ch[i] = pcm[i] / 32768
        const node = ctx.createBufferSource()
        node.buffer = buf; node.connect(ctx.destination)
        playAt = Math.max(playAt, ctx.currentTime)
        node.start(playAt); playAt += buf.duration
      }
      ws.onerror = () => { onError('Microphone connection failed.'); stop() }
      ws.onclose = () => stop()
    } catch (e) {
      onError(e.name === 'NotAllowedError' ? 'Microphone permission denied.' : e.message)
      stop()
    }
  }

  return (
    <>
      {state === 'idle' ? (
        <button className="btn ghost" disabled={disabled} onClick={start}>Talk to it</button>
      ) : (
        <button className="btn danger" onClick={stop}>{state === 'connecting' ? 'Connecting…' : 'End test call'}</button>
      )}
      {!!lines.length && (
        <div style={{ flexBasis: '100%', marginTop: 12 }}>
          <div style={{ ...preStyle, maxHeight: 220 }}>
            {lines.map(([who, text], i) => (
              <div key={i} style={{ marginBottom: 4 }}>
                <span style={{ color: who === 'agent' ? 'var(--green)' : who === 'tool' ? 'var(--amber)' : 'var(--blue)' }}>
                  {who === 'agent' ? 'Agent' : who === 'tool' ? 'Tool' : 'You'}:
                </span>{' '}{text}
              </div>
            ))}
          </div>
        </div>
      )}
    </>
  )
}
