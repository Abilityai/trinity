/**
 * The metric declaration form's pure half (trinity-enterprise#1006).
 *
 * Plain-word choices for the template's machine values, the form ⇄ payload
 * mapping, and the read-only YAML preview. The backend owns validation (the
 * same parser a hand edit goes through) and returns named per-field errors;
 * the only rules repeated here are the ones that make the form usable before
 * it is submitted (a name suggestion, the stale help line).
 */

export const MAX_DECLARED_METRICS = 50

export const TYPE_OPTIONS = [
  { value: 'counter', title: 'Count', hint: 'Things that happened, like deals closed' },
  { value: 'gauge', title: 'Level', hint: 'A reading right now, like open tickets' },
  { value: 'percentage', title: 'Percentage', hint: '0 to 100, like close rate' },
  { value: 'status', title: 'Status', hint: 'One of a few states, like Healthy or Down' },
  { value: 'duration', title: 'Duration', hint: 'A length of time, like reply time' },
  { value: 'bytes', title: 'Data size', hint: 'Bytes, like storage used' },
]

export const CADENCE_OPTIONS = [
  { value: '15m', label: 'Every 15 minutes', stale: '30 minutes' },
  { value: '1h', label: 'Every hour', stale: '2 hours' },
  { value: '6h', label: 'Every 6 hours', stale: '12 hours' },
  { value: '1d', label: 'Daily', stale: '2 days' },
  { value: '1w', label: 'Weekly', stale: '2 weeks' },
  { value: '4w', label: 'Every 4 weeks', stale: '8 weeks' },
]
export const CUSTOM_CADENCE = 'custom'

export const DIRECTION_OPTIONS = [
  { value: 'up_good', label: 'Higher is better' },
  { value: 'down_good', label: 'Lower is better' },
  { value: 'neutral', label: 'Neither' },
]

export const AGGREGATION_OPTIONS = [
  { value: 'last', label: 'The latest value' },
  { value: 'sum', label: 'The sum' },
  { value: 'avg', label: 'The average' },
]

export const STATUS_COLORS = ['green', 'yellow', 'red', 'gray', 'blue', 'orange', 'purple']

export function typeTitle(type) {
  return TYPE_OPTIONS.find((t) => t.value === type)?.title || type
}

/** A name suggestion made from the label: `Close rate` → `close_rate`. */
export function nameFromLabel(label) {
  return String(label || '')
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, '_')
    .replace(/^[^a-z]+/, '')
    .replace(/_+$/, '')
    .slice(0, 64)
}

/** The help line under the cadence select. */
export function staleHelp(cadenceChoice) {
  if (cadenceChoice === CUSTOM_CADENCE) {
    return 'Any fixed length from 1 minute to 366 days, like 12h or 3d. '
      + 'Months and years vary in length, so use 4w for monthly.'
  }
  const option = CADENCE_OPTIONS.find((c) => c.value === cadenceChoice)
  return option ? `Shown as stale if no value arrives within ${option.stale}.` : ''
}

/** An empty form, pre-filled with a working proposal (principle 3). */
export function blankForm() {
  return {
    label: '',
    name: '',
    nameEdited: false,
    type: 'counter',
    cadenceChoice: '1d',
    customCadence: '',
    unit: '',
    direction: 'up_good',
    description: '',
    recordedBy: '',
    warning: '',
    critical: '',
    dimensions: '',
    aggregation: 'last',
    values: [
      { value: 'healthy', color: 'green', label: 'Healthy' },
      { value: 'down', color: 'red', label: 'Down' },
    ],
  }
}

/** A declaration from the editor read → the form's state. */
export function formFromDeclaration(decl) {
  const preset = CADENCE_OPTIONS.find((c) => c.value === decl.cadence)
  return {
    ...blankForm(),
    label: decl.label || '',
    name: decl.name || '',
    nameEdited: true,
    type: decl.type || 'counter',
    cadenceChoice: preset ? preset.value : (decl.cadence ? CUSTOM_CADENCE : '1d'),
    customCadence: preset ? '' : (decl.cadence || ''),
    unit: decl.unit || '',
    direction: decl.direction || 'up_good',
    description: decl.description || '',
    recordedBy: decl.recorded_by || '',
    warning: decl.warning_threshold ?? '',
    critical: decl.critical_threshold ?? '',
    dimensions: Array.isArray(decl.dimensions) ? decl.dimensions.join(', ') : '',
    aggregation: decl.aggregation || 'last',
    values: Array.isArray(decl.values) && decl.values.length
      ? decl.values.map((v) => ({ value: v?.value ?? '', color: v?.color ?? 'gray', label: v?.label ?? '' }))
      : blankForm().values,
  }
}

function number(raw) {
  if (raw === '' || raw === null || raw === undefined) return { value: null, ok: true }
  const n = Number(String(raw).trim())
  return Number.isFinite(n) ? { value: n, ok: true } : { value: null, ok: false }
}

/**
 * The form's state → `{ payload, errors }`. `errors` holds only what can be
 * checked before a round trip (a threshold that is not a number); everything
 * else is the backend's to name.
 */
export function payloadFromForm(form) {
  const errors = {}
  const isStatus = form.type === 'status'
  const cadence = form.cadenceChoice === CUSTOM_CADENCE ? form.customCadence.trim() : form.cadenceChoice
  const warning = number(form.warning)
  const critical = number(form.critical)
  if (!isStatus && !warning.ok) errors.warning_threshold = 'Enter a plain number, for example 25.'
  if (!isStatus && !critical.ok) errors.critical_threshold = 'Enter a plain number, for example 20.'
  const dimensions = String(form.dimensions || '')
    .split(',')
    .map((d) => d.trim())
    .filter(Boolean)
  const payload = {
    name: (form.name || '').trim(),
    type: form.type,
    label: form.label.trim() || null,
    description: form.description.trim() || null,
    unit: isStatus ? null : (form.unit.trim() || null),
    cadence: cadence || null,
    direction: isStatus ? null : form.direction,
    aggregation: dimensions.length && form.aggregation !== 'last' ? form.aggregation : null,
    values: isStatus
      ? form.values
        .filter((v) => String(v.value || '').trim())
        .map((v) => ({ value: v.value.trim(), color: v.color || null, label: (v.label || '').trim() || null }))
      : null,
    dimensions: dimensions.length ? dimensions : null,
    warning_threshold: isStatus ? null : warning.value,
    critical_threshold: isStatus ? null : critical.value,
    recorded_by: form.recordedBy.trim() || null,
  }
  return { payload, errors }
}

function yamlScalar(value) {
  const s = String(value)
  return /^[A-Za-z0-9 _.,()%/-]*$/.test(s) && s.trim() === s && s !== '' && !/^[-?]/.test(s)
    ? s
    : JSON.stringify(s)
}

/**
 * A read-only preview of the entry this form writes. Illustrative: the backend
 * serialises the entry itself, so quoting may differ in the file.
 */
export function previewLines(payload) {
  const lines = [`- name: ${payload.name || '…'}`, `  type: ${payload.type}`]
  const scalar = (key, value) => {
    if (value !== null && value !== undefined && value !== '') lines.push(`  ${key}: ${yamlScalar(value)}`)
  }
  scalar('label', payload.label)
  scalar('description', payload.description)
  scalar('unit', payload.unit)
  lines.push(`  cadence: ${payload.cadence ? yamlScalar(payload.cadence) : '…'}`)
  scalar('direction', payload.direction)
  scalar('aggregation', payload.aggregation)
  if (payload.values?.length) {
    lines.push('  values:')
    for (const v of payload.values) {
      lines.push(`    - value: ${yamlScalar(v.value)}`)
      if (v.color) lines.push(`      color: ${v.color}`)
      if (v.label) lines.push(`      label: ${yamlScalar(v.label)}`)
    }
  }
  if (payload.dimensions?.length) {
    lines.push('  dimensions:')
    for (const d of payload.dimensions) lines.push(`    - ${yamlScalar(d)}`)
  }
  if (payload.warning_threshold !== null && payload.warning_threshold !== undefined) {
    lines.push(`  warning_threshold: ${payload.warning_threshold}`)
  }
  if (payload.critical_threshold !== null && payload.critical_threshold !== undefined) {
    lines.push(`  critical_threshold: ${payload.critical_threshold}`)
  }
  scalar('x-recorded-by', payload.recorded_by)
  return lines
}

/** Line diff of two previews for the edit form: `[{ kind: ' '|'-'|'+', text }]`. */
export function diffPreview(before, after) {
  const out = []
  let i = 0
  let j = 0
  while (i < before.length || j < after.length) {
    if (i < before.length && j < after.length && before[i] === after[j]) {
      out.push({ kind: ' ', text: before[i] })
      i += 1
      j += 1
    } else if (i < before.length && !after.slice(j).includes(before[i])) {
      out.push({ kind: '-', text: before[i] })
      i += 1
    } else {
      out.push({ kind: '+', text: after[j] })
      j += 1
    }
  }
  return out
}

/** A backend refusal → `{ code, message, fieldErrors }` for the form. */
export function readRefusal(error) {
  const detail = error?.response?.data?.detail
  if (detail && typeof detail === 'object' && !Array.isArray(detail)) {
    const fieldErrors = {}
    for (const e of detail.errors || []) {
      if (!e?.field) continue
      fieldErrors[e.field] = fieldErrors[e.field] ? `${fieldErrors[e.field]} ${e.message}` : e.message
    }
    return { code: detail.code || 'error', message: detail.message || 'The change was not saved.', fieldErrors, detail }
  }
  if (Array.isArray(detail)) {
    return { code: 'request_invalid', message: 'Some fields are not in the expected format.', fieldErrors: {}, detail }
  }
  return {
    code: 'error',
    message: typeof detail === 'string' ? detail : (error?.message || 'The change was not saved.'),
    fieldErrors: {},
    detail: null,
  }
}

/** Plain-words copy for the refusals that replace the form body. */
export const BLOCKING_REFUSALS = {
  agent_not_running: {
    title: 'Start the agent to change its metrics',
    body: 'Metrics are saved in template.yaml inside the agent, so it has to be running. Nothing was changed.',
  },
  template_unreadable: {
    title: "template.yaml couldn't be read",
    body: 'It may have a YAML syntax error. Nothing was changed, and no metric was retired. Fix the file in Files, then try again.',
  },
  template_not_editable: {
    title: 'Edit this block in the file',
    body: "This agent's metrics: block uses a YAML layout the form can't change without risking the rest of the file, "
      + 'such as anchors or one-line lists. Edit it in Files, or switch it to the plain list layout.',
  },
}
