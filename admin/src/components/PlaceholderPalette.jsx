// Click a placeholder to drop it into the prompt at the cursor. The list comes from the
// API (prompt_render.KNOWN_PLACEHOLDERS), so the palette and the save-time validator can
// never disagree about what is spellable.
const GROUPS = [
  ['The helpline', ['helpline_name', 'company_name']],
  ['Routing (rendered per call)', ['category_list', 'department_list', 'language_list']],
  ['The caller', ['caller_phone', 'caller_phone_spoken']],
  ['Today', ['today_spoken', 'today_iso', 'now_time']],
]

const HINT = {
  helpline_name: 'EPP_HELPLINE_NAME from .env',
  company_name: 'EPP_COMPANY_NAME from .env',
  category_list: 'One line per caller type, from the Routing page',
  department_list: 'Active departments, comma-separated',
  language_list: 'The enabled languages, spoken form',
  caller_phone: 'The caller ID in E.164',
  caller_phone_spoken: 'The caller ID digit by digit',
  today_spoken: '"the twentieth of September"',
  today_iso: '2026-09-20',
  now_time: '"half past ten in the morning"',
}

export default function PlaceholderPalette({ known, onInsert }) {
  const allowed = new Set(known || [])
  return (
    <div>
      <div className="muted" style={{ fontSize: '0.78rem', marginBottom: 10 }}>
        Click to insert. Anything with no value is left out of the sentence rather than spoken as a gap.
      </div>
      {GROUPS.map(([label, keys]) => {
        const usable = keys.filter((k) => allowed.has(k))
        if (!usable.length) return null
        return (
          <div key={label} style={{ marginBottom: 12 }}>
            <div style={{ fontSize: '0.72rem', textTransform: 'uppercase', letterSpacing: '.05em',
                          color: 'var(--secondary)', marginBottom: 5 }}>{label}</div>
            <div style={{ display: 'flex', flexWrap: 'wrap', gap: 5 }}>
              {usable.map((k) => (
                <button key={k} type="button" className="btn ghost sm"
                        style={{ fontFamily: 'var(--mono)', fontSize: '0.72rem', padding: '3px 7px' }}
                        onClick={() => onInsert(`{${k}}`)} title={HINT[k] || `Insert {${k}}`}>
                  {k}
                </button>
              ))}
            </div>
          </div>
        )
      })}
    </div>
  )
}
