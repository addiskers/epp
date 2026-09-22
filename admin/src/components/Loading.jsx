// A loader that never moves the layout: a small spinner with a label, rendered in a fixed
// slot (the toolbar) while a table refreshes. The table underneath keeps its rows and dims.
export default function InlineLoader({ show, label = 'Loading…' }) {
  return (
    <span className={`inline-loader ${show ? 'on' : ''}`} aria-live="polite" aria-busy={show}>
      <span className="spinner" />
      <span>{label}</span>
    </span>
  )
}
