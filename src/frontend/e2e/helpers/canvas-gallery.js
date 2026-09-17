// The canvas gallery (#2583): every block layout an agent is told it can
// produce (the platform prompt's kinds, the rich fences, the design kit, the
// four starter layouts) plus the adversarial shapes an agent will eventually
// emit — long, wide, empty, malformed, foreign-script. Seeded through the REAL
// write route (`PUT /api/agents/{name}/canvas/{id}`) so the spec proves what
// an agent can actually produce renders, not what a hand-mounted prop would.
//
// Pure data, the seeding helpers, and the three page-side helpers every
// gallery spec shares (`measureKit`, `settled`, `unclip`). The assertions live
// in `canvas-gallery*.spec.js`; keep this file free of Playwright imports so
// the same fixture can be replayed against any instance with a token.

import zlib from 'node:zlib'

const AGENT = process.env.CANVAS_AGENT || 'test-harness-agent'
const PREFIX = 'g-'

// ---------------------------------------------------------------- helpers

function days(n, start = '2026-08-01') {
  const t0 = Date.parse(start)
  return Array.from({ length: n }, (_, i) => new Date(t0 + i * 86400000).toISOString().slice(0, 10))
}
function hours(n, start = '2026-09-01T00:00:00Z') {
  const t0 = Date.parse(start)
  return Array.from({ length: n }, (_, i) => new Date(t0 + i * 3600000).toISOString())
}
function series(label, ts, fn, extra = {}) {
  return { label, points: ts.map((t, i) => ({ ts: t, value: fn(i) })), ...extra }
}
function chart(type, s, extra = {}) {
  return { type, series: s, ...extra }
}
const LONG_TOKEN = 'x'.repeat(60)
const LONG_URL = 'https://example.com/' + 'segment/'.repeat(24) + 'report.html?q=' + 'a'.repeat(80)
const LOREM = 'The quick brown fox jumps over the lazy dog while the agent keeps its canvas current. '
const PARAGRAPH_4000 = LOREM.repeat(Math.ceil(4000 / LOREM.length)).slice(0, 4000)
const CJK = '東京の天気は晴れです。今日の気温は二十八度で、湿度は六十パーセントです。明日は雨が降る予報です。'
const RTL = 'מזג האוויר בתל אביב היום בהיר, הטמפרטורה עשרים ותשע מעלות, והלחות שבעים אחוז.'
const ARABIC = 'تقرير الأسبوع: ارتفعت المبيعات بنسبة اثني عشر بالمئة مقارنة بالأسبوع الماضي.'
const EMOJI = '🚀 Launch ✅ done · 🐛 3 bugs · 📈 up 12% · 🔥🔥🔥'

// A tiny valid PNG of a given size and colour, so image blocks have real
// intrinsic dimensions without any network. Solid colour deflates to a few
// hundred bytes, which keeps even a 1200-px banner under the 64 KiB inline cap.
function crc32(buf) {
  let c, crc = 0xffffffff
  for (let n = 0; n < buf.length; n++) {
    c = (crc ^ buf[n]) & 0xff
    for (let k = 0; k < 8; k++) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1
    crc = (crc >>> 8) ^ c
  }
  return (crc ^ 0xffffffff) >>> 0
}
function be32(n) { return [(n >>> 24) & 255, (n >>> 16) & 255, (n >>> 8) & 255, n & 255] }
function chunk(type, data) {
  const body = Buffer.concat([Buffer.from(type, 'latin1'), data])
  return Buffer.concat([Buffer.from(be32(data.length)), body, Buffer.from(be32(crc32(body)))])
}
export function pngDataUri(width, height, rgb) {
  const row = Buffer.from([0, ...Array.from({ length: width }, () => rgb).flat()])
  const raw = Buffer.concat(Array.from({ length: height }, () => row))
  const ihdr = Buffer.from([...be32(width), ...be32(height), 8, 2, 0, 0, 0])
  const png = Buffer.concat([
    Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]),
    chunk('IHDR', ihdr), chunk('IDAT', zlib.deflateSync(raw)), chunk('IEND', Buffer.alloc(0)),
  ])
  return 'data:image/png;base64,' + png.toString('base64')
}

// ------------------------------------------------------------- the gallery

const D30 = days(30)
const D14 = days(14)
const H24 = hours(24)

function bigTable(rows, cols) {
  const columns = Array.from({ length: cols }, (_, i) => `Column ${i + 1}`)
  return {
    columns,
    rows: Array.from({ length: rows }, (_, r) => columns.map((_, c) => (c === 0 ? `Row ${r + 1}` : (r * 7 + c * 13) % 997))),
  }
}

export const GALLERY = [
  // 1 ───────────────────────────────────────────────── tables
  {
    canvas_id: `${PREFIX}tables`, title: 'Tables', blocks: [
      { id: 'narrow', kind: 'table', title: 'Two columns', payload: { columns: ['Name', 'Status'], rows: [['Acme', 'qualified'], ['Globex', 'won'], ['Initech', 'lost']] } },
      { id: 'wide', kind: 'table', title: 'Twelve columns', payload: bigTable(6, 12) },
      { id: 'tall', kind: 'table', title: '200 rows', payload: bigTable(200, 4) },
      { id: 'token', kind: 'table', title: 'Unbroken 60-char token in a cell', payload: { columns: ['Key', 'Value'], rows: [['hash', LONG_TOKEN], ['url', LONG_URL]] } },
      { id: 'sparse', kind: 'table', title: 'Empty cells, nulls, nested objects', payload: { columns: ['A', 'B', 'C'], rows: [{ A: 1 }, { A: null, B: '', C: { nested: true } }, [1, 2], []] } },
      { id: 'objects', kind: 'table', title: 'Object rows', payload: { columns: ['name', 'score'], rows: [{ name: 'alpha', score: 0.91 }, { name: 'beta', score: 0.42 }] } },
      { id: 'empty', kind: 'table', title: 'Columns, no rows', payload: { columns: ['Nothing', 'Here'], rows: [] } },
      { id: 'i18n', kind: 'table', title: 'CJK · RTL · emoji cells', payload: { columns: ['City', 'Report'], rows: [['東京', CJK], ['תל אביב', RTL], ['الرياض', ARABIC], ['Launch', EMOJI]] } },
    ],
  },
  // 2 ───────────────────────────────────────────────── kpis
  {
    canvas_id: `${PREFIX}kpis`, title: 'KPI tiles', blocks: [
      { id: 'three', kind: 'kpi', title: 'Three tiles', payload: { tiles: [{ label: 'Leads', value: 14, unit: 'new' }, { label: 'Won', value: 7 }, { label: 'Rate', value: '50%' }] } },
      { id: 'one', kind: 'kpi', title: 'One tile', payload: { tiles: [{ label: 'Uptime', value: '99.98%' }] } },
      { id: 'twelve', kind: 'kpi', title: 'Twelve tiles', payload: { tiles: Array.from({ length: 12 }, (_, i) => ({ label: `Metric ${i + 1}`, value: (i + 1) * 1234, unit: i % 3 ? '' : 'ms' })) } },
      { id: 'long', kind: 'kpi', title: 'Long label, long value, long unit', payload: { tiles: [{ label: 'Average time to first response across all inbound channels this quarter', value: 1234567890123, unit: 'milliseconds per conversation' }, { label: LONG_TOKEN, value: LONG_TOKEN }] } },
      { id: 'odd', kind: 'kpi', title: 'Object, null, boolean values', payload: { tiles: [{ label: 'Object', value: { a: 1 } }, { label: 'Null', value: null }, { label: 'Bool', value: true }, { label: '' }] } },
      { id: 'i18n', kind: 'kpi', title: 'Non-Latin tiles', payload: { tiles: [{ label: '気温', value: '二十八', unit: '度' }, { label: 'לחות', value: '70%' }, { label: '🔥 streak', value: 12, unit: 'days' }] } },
    ],
  },
  // 3 ───────────────────────────────────────────────── charts: line / area
  {
    canvas_id: `${PREFIX}charts-lines`, title: 'Charts — line & area', blocks: [
      { id: 'two', kind: 'chart', title: 'Line, two series, 30 days', payload: chart('line', [series('Visits', D30, (i) => 100 + Math.round(40 * Math.sin(i / 3))), series('Signups', D30, (i) => 10 + (i % 7))]) },
      { id: 'area', kind: 'chart', title: 'Area, one series, unit', payload: chart('area', [series('Revenue', D30, (i) => 1000 + i * 37, { unit: '€' })]) },
      { id: 'twelve', kind: 'chart', title: 'Line, twelve series (palette wraps)', payload: chart('line', Array.from({ length: 12 }, (_, s) => series(`Series ${s + 1}`, D14, (i) => (s + 1) * 10 + i * (s % 3)))) },
      { id: 'gaps', kind: 'chart', title: 'Nulls and missing points', payload: chart('line', [{ label: 'Gappy', points: D14.map((t, i) => ({ ts: t, value: i % 4 === 0 ? null : i * 3 })) }, { label: 'Sparse', points: [{ ts: D14[2], value: 5 }, { ts: D14[9], value: 40 }] }]) },
      { id: 'point', kind: 'chart', title: 'Single point', payload: chart('line', [{ label: 'Once', points: [{ ts: '2026-09-01', value: 42 }] }]) },
      { id: 'negative', kind: 'chart', title: 'Negative values', payload: chart('line', [series('Delta', D14, (i) => (i - 7) * 13)]) },
      { id: 'hourly', kind: 'chart', title: 'Hourly (day + time labels)', payload: chart('area', [series('CPU %', H24, (i) => 30 + Math.round(25 * Math.sin(i / 2)), { unit: '%' })]) },
      { id: 'categories', kind: 'chart', title: 'Category axis, 30 categories', payload: chart('line', [{ label: 'Score', points: Array.from({ length: 30 }, (_, i) => ({ ts: `Category number ${i + 1}`, value: (i * 17) % 50 })) }]) },
      { id: 'zeros', kind: 'chart', title: 'All zero', payload: chart('line', [series('Flat', D14, () => 0)]) },
      { id: 'stale', kind: 'chart', title: 'Stale series with last_point_at', payload: chart('line', [series('Old', D14, (i) => i, { stale: true, last_point_at: '2026-08-14T09:00:00Z' })]) },
      { id: 'longlabels', kind: 'chart', title: 'Very long series labels', payload: chart('line', [series('A'.repeat(120), D14, (i) => i), series(LONG_URL, D14, (i) => 14 - i)]) },
      { id: 'strings', kind: 'chart', title: 'Numeric strings and junk values', payload: chart('line', [{ label: 'Mixed', points: [{ ts: D14[0], value: '12' }, { ts: D14[1], value: 'abc' }, { ts: D14[2], value: 7 }, { ts: D14[3], value: Infinity }] }]) },
    ],
  },
  // 4 ───────────────────────────────────────────────── charts: bars / pies
  {
    canvas_id: `${PREFIX}charts-bars`, title: 'Charts — bars & pies', blocks: [
      { id: 'byregion', kind: 'chart', title: 'Bar, one point per series (leads by region)', payload: chart('bar', ['EMEA', 'APAC', 'NA', 'LATAM'].map((r, i) => ({ label: r, points: [{ ts: 'W36', value: (i + 1) * 11 }] }))) },
      { id: 'stacked', kind: 'chart', title: 'Stacked, three series, 14 days', payload: chart('stacked_bar', [series('Email', D14, (i) => 5 + i), series('Chat', D14, (i) => 3 + (i % 5)), series('Voice', D14, (i) => i % 3)]) },
      { id: 'twelvebars', kind: 'chart', title: 'Twelve bars, long category labels', payload: chart('bar', Array.from({ length: 12 }, (_, i) => ({ label: `Region with a rather long name number ${i + 1}`, points: [{ ts: 'x', value: (i * 31) % 47 + 1 }] }))) },
      { id: 'negbar', kind: 'chart', title: 'Bars with negatives (clamped)', payload: chart('bar', [series('Net', D14, (i) => (i - 7) * 4)]) },
      { id: 'sixty', kind: 'chart', title: 'Sixty columns', payload: chart('stacked_bar', [series('A', days(60), (i) => i % 9), series('B', days(60), (i) => (i * 3) % 7)]) },
      { id: 'pie', kind: 'chart', title: 'Pie, five slices', payload: chart('pie', ['Won', 'Lost', 'Open', 'Stalled', 'New'].map((l, i) => ({ label: l, points: [{ ts: 'now', value: [40, 25, 20, 10, 5][i] }] }))) },
      { id: 'donut', kind: 'chart', title: 'Donut, twelve slices', payload: chart('donut', Array.from({ length: 12 }, (_, i) => ({ label: `Slice ${i + 1}`, points: [{ ts: 'now', value: i + 1 }], unit: 'GB' }))) },
      { id: 'oneslice', kind: 'chart', title: 'Pie, one slice', payload: chart('pie', [{ label: 'Everything', points: [{ ts: 'now', value: 1 }] }]) },
      { id: 'zeropie', kind: 'chart', title: 'Pie of zeros', payload: chart('pie', [{ label: 'Nil', points: [{ ts: 'now', value: 0 }] }, { label: 'Nada', points: [{ ts: 'now', value: 0 }] }]) },
      { id: 'legacy', kind: 'chart', title: 'Legacy shape {labels, series[{data}]}', payload: { type: 'bar', labels: D14, series: [{ label: 'Old shape', data: D14.map((_, i) => i * 2) }] } },
      { id: 'noseries', kind: 'chart', title: 'No series (falls back to JSON)', payload: { type: 'line', note: 'nothing to plot' } },
      { id: 'badtype', kind: 'chart', title: 'Unknown type (defaults to line)', payload: chart('radar', [series('X', D14, (i) => i)]) },
      { id: 'colors', kind: 'chart', title: 'Named and bad colours', payload: chart('bar', [{ label: 'Hex', color: '#e11d48', points: [{ ts: 'a', value: 3 }] }, { label: 'Bad', color: 'url(javascript:1)', points: [{ ts: 'a', value: 5 }] }, { label: 'Word', color: 'red', points: [{ ts: 'a', value: 2 }] }]) },
    ],
  },
  // 5 ───────────────────────────────────────────────── markdown prose
  {
    canvas_id: `${PREFIX}markdown`, title: 'Markdown', blocks: [
      { id: 'basic', kind: 'markdown', title: 'Headings, lists, quote, link, inline code', payload: { markdown: '# Weekly findings\n\n## Summary\n\nThree things moved this week — see the [tracker](https://example.com) and `deploy.sh`.\n\n- First item\n  - nested item\n  - another nested item\n- Second item\n\n1. Ordered one\n2. Ordered two\n\n> A quote that runs a little long so it wraps in the narrow column and shows the quote chrome.\n\n---\n\nDone.' } },
      { id: 'long', kind: 'markdown', title: '4000-character paragraph', payload: { markdown: PARAGRAPH_4000 } },
      { id: 'url', kind: 'markdown', title: 'Unbroken URL and token', payload: { markdown: `Source: ${LONG_URL}\n\nHash: ${LONG_TOKEN}${LONG_TOKEN}` } },
      { id: 'i18n', kind: 'markdown', title: 'CJK, RTL, Arabic, emoji', payload: { markdown: `${CJK}\n\n${RTL}\n\n${ARABIC}\n\n${EMOJI}` } },
      { id: 'headings', kind: 'markdown', title: 'Headings only', payload: { markdown: '# One\n## Two\n### Three\n#### Four' } },
      { id: 'mdtable', kind: 'markdown', title: 'Wide markdown table', payload: { markdown: '| ' + Array.from({ length: 10 }, (_, i) => `Column ${i + 1}`).join(' | ') + ' |\n|' + ' --- |'.repeat(10) + '\n' + Array.from({ length: 5 }, (_, r) => '| ' + Array.from({ length: 10 }, (_, c) => `cell ${r}-${c} with words`).join(' | ') + ' |').join('\n') } },
      { id: 'code', kind: 'markdown', title: 'Code block with a long line', payload: { markdown: '```bash\ncurl -s -H "Authorization: Bearer ${TOKEN}" https://example.com/api/agents/very-long-agent-name/canvas/main?include=blocks&format=json&pretty=true&another=parameter\n```\n\nInline `code` after.' } },
      { id: 'image', kind: 'markdown', title: 'Inline image (data URI)', payload: { markdown: `![wide](${pngDataUri(600, 80, [99, 102, 241])})\n\nCaption under the image.` } },
      { id: 'empty', kind: 'markdown', title: 'Empty markdown', payload: { markdown: '' } },
      { id: 'notstring', kind: 'markdown', title: 'Markdown that is not a string', payload: { markdown: 42 } },
      { id: 'htmlish', kind: 'markdown', title: 'Raw HTML inside markdown (script, style, iframe)', payload: { markdown: 'Before <script>alert(1)</script> <style>body{display:none}</style> <iframe src="https://example.com"></iframe> after.\n\n<div class="ck-callout ck-warning">A kit callout written as raw HTML in markdown.</div>' } },
    ],
  },
  // 6 ───────────────────────────────────────────────── markdown with rich fences
  {
    canvas_id: `${PREFIX}fences`, title: 'Markdown — rich fences', blocks: [
      { id: 'all', kind: 'markdown', title: 'Chart, KPI, table and mermaid in one page', payload: { markdown: '## Explainer\n\nA paragraph first.\n\n```kpi\n' + JSON.stringify({ tiles: [{ label: 'Open', value: 42 }, { label: 'Won', value: 7 }, { label: 'Lost', value: 3 }] }) + '\n```\n\nThen the trend:\n\n```chart\n' + JSON.stringify(chart('area', [series('Leads', D14, (i) => 10 + i)])) + '\n```\n\nAnd the breakdown:\n\n```table\n' + JSON.stringify({ columns: ['Region', 'Leads'], rows: [['EMEA', 12], ['APAC', 9]] }) + '\n```\n\n```mermaid\ngraph LR; A[Inbound] --> B{Qualified?}; B -->|yes| C[Won]; B -->|no| D[Lost]\n```\n\nClosing paragraph.' } },
      { id: 'broken', kind: 'markdown', title: 'Fence with broken JSON stays a code block', payload: { markdown: 'Look:\n\n```chart\n{"type": "line", "series": [\n```\n\nand an unterminated one:\n\n```kpi\n{"tiles": []}' } },
      { id: 'nested', kind: 'markdown', title: 'A ```chart shown inside a ````markdown example is not extracted', payload: { markdown: '````markdown\n```chart\n{"type":"line","series":[{"label":"x","points":[{"ts":"a","value":1}]}]}\n```\n````' } },
      { id: 'kit', kind: 'markdown', title: 'Kit markup inside markdown', payload: { markdown: '<div class="ck-section"><h2 class="ck-section-title">Pipeline</h2><p class="ck-section-sub">Week 36</p></div>\n\n<div class="ck-grid-3"><div class="ck-kpi"><p class="ck-kpi-label">Open</p><p class="ck-kpi-value">42<span class="ck-kpi-unit">deals</span></p><p class="ck-kpi-delta ck-up">12%</p></div><div class="ck-kpi"><p class="ck-kpi-label">Won</p><p class="ck-kpi-value">7</p><p class="ck-kpi-delta ck-down">3%</p></div><div class="ck-kpi"><p class="ck-kpi-label">Lost</p><p class="ck-kpi-value">3</p><p class="ck-kpi-delta ck-flat">0%</p></div></div>\n\nProse continues **after** the kit markup.' } },
    ],
  },
  // 7 ───────────────────────────────────────────────── html (voice panel + kit)
  {
    canvas_id: `${PREFIX}html`, title: 'HTML blocks', blocks: [
      { id: 'voice', kind: 'html', title: 'What the voice panel writes', payload: { html: '<h2>Call notes</h2><ul><li>Agreed on the Q4 plan</li><li>Follow up Tuesday</li></ul><table><tr><th>Item</th><th>Owner</th></tr><tr><td>Deck</td><td>Ana</td></tr></table>' } },
      { id: 'card', kind: 'html', title: 'Kit card with chips', payload: { html: '<div class="ck-card"><div class="ck-card-title">Next actions</div><div class="ck-card-meta">updated 5 min ago</div><div class="ck-row"><span class="ck-chip ck-warning">2 stalled</span><span class="ck-chip ck-success">5 on track</span><span class="ck-chip ck-danger">1 blocked</span><span class="ck-chip ck-info">info</span><span class="ck-chip">neutral</span></div></div>' } },
      { id: 'grid4', kind: 'html', title: 'ck-grid-4 of KPI tiles', payload: { html: '<div class="ck-grid-4">' + Array.from({ length: 8 }, (_, i) => `<div class="ck-kpi"><p class="ck-kpi-label">Metric ${i + 1}</p><p class="ck-kpi-value">${(i + 1) * 317}<span class="ck-kpi-unit">ms</span></p></div>`).join('') + '</div>' } },
      { id: 'table', kind: 'html', title: 'ck-table-wrap, wide and tall', payload: { html: '<div class="ck-table-wrap"><table class="ck-table"><thead><tr>' + Array.from({ length: 10 }, (_, i) => `<th>Column ${i + 1}</th>`).join('') + '</tr></thead><tbody>' + Array.from({ length: 40 }, (_, r) => '<tr>' + Array.from({ length: 10 }, (_, c) => `<td class="${c ? 'ck-num' : ''}">${c ? (r * 13 + c) % 1000 : 'Row ' + r}</td>`).join('') + '</tr>').join('') + '</tbody></table></div>' } },
      { id: 'callouts', kind: 'html', title: 'Callouts in every tone', payload: { html: ['info', 'success', 'warning', 'danger', 'neutral'].map((t) => `<div class="ck-callout ck-${t}"><strong>${t}</strong> — a callout that runs long enough to wrap in the rail column so the tone ground is visible behind two lines.</div>`).join('') } },
      { id: 'longline', kind: 'html', title: 'Unwrapped long line and long word', payload: { html: `<p>${LONG_TOKEN}${LONG_TOKEN}${LONG_TOKEN}</p><pre>${'one very long preformatted line that goes on and on '.repeat(6)}</pre>` } },
      { id: 'bomb', kind: 'html', title: 'width: 9999px on a card (bounded)', payload: { html: '<div class="ck-card" style="width: 9999px; height: 4000px; position: fixed">Wide card</div><div class="ck-figure" style="max-width: 480px"><p>figure with max-width</p></div>' } },
      { id: 'stripped', kind: 'html', title: 'Foreign classes, style, script, id are dropped', payload: { html: '<style>.canvas-kit{display:none}</style><script>alert(1)</script><div id="app" class="bg-red-500 fixed inset-0 ck-card"><span class="text-9xl">still readable</span></div>' } },
      { id: 'nested', kind: 'html', title: 'Deep nesting', payload: { html: '<div>'.repeat(40) + 'deep' + '</div>'.repeat(40) } },
      { id: 'img', kind: 'html', title: 'Image tag in html', payload: { html: `<figure class="ck-figure"><img src="${pngDataUri(400, 300, [16, 185, 129])}" alt="green"><figcaption class="ck-caption">A 4:3 image</figcaption></figure>` } },
      { id: 'empty', kind: 'html', title: 'Empty html', payload: { html: '' } },
      { id: 'section', kind: 'html', title: 'Section header + muted/mono/small text', payload: { html: '<div class="ck-section"><h2 class="ck-section-title">Operations</h2><span class="ck-section-sub">last 24h</span></div><p class="ck-muted">muted</p><p class="ck-mono">mono text 0x1F</p><p class="ck-small">small</p><p class="ck-right">right</p><p class="ck-center">center</p>' } },
    ],
  },
  // 8 ───────────────────────────────────────────────── diagrams
  {
    canvas_id: `${PREFIX}diagrams`, title: 'Diagrams', blocks: [
      { id: 'lr', kind: 'diagram', title: 'Flowchart LR', payload: { mermaid: 'graph LR; A[Inbound lead] --> B{Qualified?}; B -->|yes| C[Book call]; B -->|no| D[Nurture]; C --> E[Won]' } },
      { id: 'td', kind: 'diagram', title: 'Flowchart TD, 30 nodes', payload: { mermaid: 'graph TD;\n' + Array.from({ length: 30 }, (_, i) => `N${i}[Step ${i}] --> N${i + 1}[Step ${i + 1}]`).join('\n') } },
      { id: 'wide', kind: 'diagram', title: 'Very wide flowchart (40 nodes LR)', payload: { mermaid: 'graph LR;\n' + Array.from({ length: 40 }, (_, i) => `N${i}[Stage ${i}] --> N${i + 1}[Stage ${i + 1}]`).join('\n') } },
      { id: 'seq', kind: 'diagram', title: 'Sequence diagram', payload: { mermaid: 'sequenceDiagram\n  participant U as User\n  participant A as Agent\n  participant T as Trinity\n  U->>A: ask for a chart\n  A->>T: set_canvas(blocks)\n  T-->>A: ok\n  A-->>U: it is on the canvas' } },
      { id: 'pie', kind: 'diagram', title: 'Mermaid pie', payload: { mermaid: 'pie title Deals\n  "Won" : 40\n  "Lost" : 25\n  "Open" : 35' } },
      { id: 'gantt', kind: 'diagram', title: 'Gantt', payload: { mermaid: 'gantt\n  title Release\n  dateFormat YYYY-MM-DD\n  section Build\n  Implement :a1, 2026-09-01, 5d\n  Review :after a1, 2d\n  section Ship\n  Release :2026-09-08, 1d' } },
      { id: 'class', kind: 'diagram', title: 'Class diagram', payload: { mermaid: 'classDiagram\n  class Canvas { +id +blocks +write() }\n  class Block { +kind +payload }\n  Canvas *-- Block' } },
      { id: 'labels', kind: 'diagram', title: 'Long node labels', payload: { mermaid: `graph TD; A["${'A very long node label that keeps going '.repeat(4)}"] --> B["${LONG_TOKEN}"]` } },
      { id: 'broken', kind: 'diagram', title: 'Broken mermaid (contained error)', payload: { mermaid: 'graph TD; A --> ; this is not mermaid at all {{{' } },
      { id: 'init', kind: 'diagram', title: 'themeCSS directive is neutralised', payload: { mermaid: '%%{init: {"themeCSS": ".canvas-kit{display:none}", "theme": "forest"}}%%\ngraph LR; A --> B' } },
      { id: 'huge', kind: 'diagram', title: 'Near the 20,000-char cap', payload: { mermaid: 'graph TD;\n' + Array.from({ length: 700 }, (_, i) => `N${i} --> N${i + 1}`).join('\n') } },
    ],
  },
  // 9 ───────────────────────────────────────────────── images
  {
    canvas_id: `${PREFIX}images`, title: 'Images', blocks: [
      { id: 'wide', kind: 'image', title: 'Wide landscape (1200×200)', payload: { src: pngDataUri(1200, 200, [99, 102, 241]), caption: 'A wide banner', alt: 'wide' } },
      { id: 'tall', kind: 'image', title: 'Tall portrait (200×900)', payload: { src: pngDataUri(200, 900, [244, 63, 94]), caption: 'A tall image' } },
      { id: 'tiny', kind: 'image', title: 'Tiny (8×8)', payload: { src: pngDataUri(8, 8, [16, 185, 129]) } },
      { id: 'path', kind: 'image', title: 'Workspace path that does not exist', payload: { src: 'content/missing-chart.png', caption: 'Should say it could not be loaded' } },
      { id: 'https', kind: 'image', title: 'Remote https that 404s', payload: { src: 'https://example.invalid/does-not-exist.png', caption: 'Remote, unreachable' } },
      { id: 'longcap', kind: 'image', title: 'Long caption and alt', payload: { src: pngDataUri(300, 120, [245, 158, 11]), caption: LOREM.repeat(4), alt: LONG_TOKEN } },
    ],
  },
  // 10 ──────────────────────────────────────────────── timeline + json
  {
    canvas_id: `${PREFIX}timeline-json`, title: 'Timeline & JSON', blocks: [
      { id: 'three', kind: 'timeline', title: 'Three events', payload: { events: [{ ts: '2026-09-01T09:00:00Z', label: 'Deal closed', detail: 'Acme signed the annual plan.' }, { ts: '2026-09-02T10:30:00Z', label: 'Kickoff' }, { label: 'No timestamp' }] } },
      { id: 'forty', kind: 'timeline', title: 'Forty events with long detail', payload: { events: Array.from({ length: 40 }, (_, i) => ({ ts: `2026-08-${String((i % 28) + 1).padStart(2, '0')}T12:00:00Z`, label: `Event ${i + 1}`, detail: i % 5 === 0 ? LOREM.repeat(3) : 'short' })) } },
      { id: 'odd', kind: 'timeline', title: 'Non-string fields', payload: { events: [{ ts: 12345, label: { nested: true }, detail: ['a', 'b'] }, { ts: null, label: null }, 'not-an-object', null] } },
      { id: 'empty', kind: 'timeline', title: 'No events', payload: { events: [] } },
      { id: 'obj', kind: 'json', title: 'JSON object', payload: { status: 'ok', counts: { open: 42, won: 7 }, tags: ['a', 'b'], note: LOREM } },
      { id: 'arr', kind: 'json', title: 'JSON array', payload: [1, 2, 3, { four: 4 }] },
      { id: 'str', kind: 'json', title: 'JSON with a long unbroken string', payload: { hash: LONG_TOKEN + LONG_TOKEN + LONG_TOKEN } },
      { id: 'deep', kind: 'json', title: 'Deeply nested JSON', payload: JSON.parse('{"a":'.repeat(30) + '1' + '}'.repeat(30)) },
    ],
  },
  // 11 ──────────────────────────────────────────────── degradation
  // (An unknown `kind` is refused by name at write — a 422 naming the nine
  // kinds — and so is a diagram with blank source (400); neither can be
  // seeded, so `blockRenderer` / `CanvasBlock` cover them in the unit suite.)
  {
    canvas_id: `${PREFIX}degrade`, title: 'Degradation', blocks: [
      { id: 'mismatch', kind: 'chart', title: 'chart kind with a kpi payload', payload: { tiles: [{ label: 'x', value: 1 }] } },
      { id: 'tablebad', kind: 'table', title: 'table with rows not an array', payload: { columns: ['a'], rows: 'nope' } },
      { id: 'kpibad', kind: 'kpi', title: 'kpi with tiles not an array', payload: { tiles: { label: 'x' } } },
      { id: 'empty', kind: 'markdown', title: 'Empty payload', payload: {} },
      { id: 'nullish', kind: 'timeline', title: 'Payload with null fields', payload: { events: null } },
      { id: 'imgnosrc', kind: 'image', title: 'Image with a data URI that is not an image', payload: { src: pngDataUri(4, 4, [0, 0, 0]).replace('image/png', 'image/webp') } },
      { id: 'longtitle', kind: 'kpi', title: 'T'.repeat(300), payload: { tiles: [{ label: 'ok', value: 1 }] } },
      { id: 'notitle', kind: 'kpi', payload: { tiles: [{ label: 'no title above me', value: 2 }] } },
      { id: 'htmlnum', kind: 'html', title: 'html that is a number', payload: { html: 12345 } },
    ],
  },
  // 12 ──────────────────────────────────────────────── the ceiling: 50 blocks
  {
    canvas_id: `${PREFIX}fifty`, title: 'Fifty blocks', blocks: Array.from({ length: 50 }, (_, i) => {
      const kinds = ['kpi', 'table', 'markdown', 'chart', 'timeline']
      const kind = kinds[i % kinds.length]
      const payloads = {
        kpi: { tiles: [{ label: `Block ${i}`, value: i * 3 }] },
        table: { columns: ['n', 'sq'], rows: [[i, i * i]] },
        markdown: { markdown: `Block **${i}** of fifty.` },
        chart: chart('bar', [{ label: `B${i}`, points: [{ ts: 'a', value: i + 1 }] }, { label: 'C', points: [{ ts: 'a', value: 50 - i }] }]),
        timeline: { events: [{ ts: '2026-09-01', label: `Event ${i}` }] },
      }
      return { id: `b${i}`, kind, title: i % 7 === 0 ? `Block ${i}` : undefined, payload: payloads[kind] }
    }),
  },
  // 13 ──────────────────────────────────────────────── layouts
  {
    canvas_id: `${PREFIX}layout-dashboard`, title: 'Layout — dashboard', template: 'dashboard', blocks: [
      { id: 'h', slot: 'header', kind: 'markdown', payload: { markdown: '## Pipeline · W36\n\nUpdated by the agent every hour.' } },
      { id: 'k1', slot: 'kpis', kind: 'kpi', payload: { tiles: [{ label: 'Open', value: 42 }, { label: 'Won', value: 7 }] } },
      { id: 'k2', slot: 'kpis', kind: 'kpi', payload: { tiles: [{ label: 'Lost', value: 3 }] } },
      { id: 'k3', slot: 'kpis', kind: 'chart', payload: chart('donut', [{ label: 'Won', points: [{ ts: 'a', value: 7 }] }, { label: 'Lost', points: [{ ts: 'a', value: 3 }] }]) },
      { id: 'm', slot: 'main', kind: 'chart', title: 'Trend', payload: chart('area', [series('Leads', D30, (i) => 10 + i)]) },
      { id: 's', slot: 'side', kind: 'table', title: 'By region', payload: { columns: ['Region', 'Leads', 'Won'], rows: [['EMEA', 12, 3], ['APAC', 9, 2], ['NA', 21, 2]] } },
      { id: 's2', slot: 'side', kind: 'html', payload: { html: '<div class="ck-card"><div class="ck-card-title">Next</div><span class="ck-chip ck-warning">2 stalled</span></div>' } },
      { id: 'f', slot: 'footer', kind: 'markdown', payload: { markdown: '_Source: CRM export, 2026-09-07._' } },
      { id: 'u', kind: 'markdown', title: 'Unslotted (renders after the layout)', payload: { markdown: 'This block has no slot.' } },
      { id: 'x', slot: 'nowhere', kind: 'kpi', title: 'Unknown slot (renders after the layout)', payload: { tiles: [{ label: 'orphan', value: 1 }] } },
    ],
  },
  {
    canvas_id: `${PREFIX}layout-report`, title: 'Layout — report', template: 'report', blocks: [
      { id: 'h', slot: 'header', kind: 'html', payload: { html: '<div class="ck-section"><h2 class="ck-section-title">Quarterly review</h2><span class="ck-section-sub">Q3 2026</span></div>' } },
      { id: 's', slot: 'summary', kind: 'html', payload: { html: '<div class="ck-callout ck-info">Revenue up 12%, churn flat, two risks flagged.</div>' } },
      { id: 'b', slot: 'body', kind: 'markdown', payload: { markdown: '### Findings\n\n' + PARAGRAPH_4000.slice(0, 1500) + '\n\n### Method\n\n' + PARAGRAPH_4000.slice(0, 600) } },
      { id: 'f1', slot: 'figures', kind: 'chart', title: 'Revenue', payload: chart('line', [series('Revenue', D30, (i) => 1000 + i * 20, { unit: '€' })]) },
      { id: 'f2', slot: 'figures', kind: 'chart', title: 'Mix', payload: chart('pie', [{ label: 'New', points: [{ ts: 'a', value: 60 }] }, { label: 'Renewal', points: [{ ts: 'a', value: 40 }] }]) },
      { id: 'f3', slot: 'figures', kind: 'diagram', title: 'Flow', payload: { mermaid: 'graph LR; A --> B --> C' } },
      { id: 'f4', slot: 'figures', kind: 'image', title: 'Banner', payload: { src: pngDataUri(800, 200, [59, 130, 246]), caption: 'wide image in a figure cell' } },
      { id: 'a', slot: 'appendix', kind: 'table', title: 'Appendix A', payload: bigTable(12, 8) },
    ],
  },
  {
    canvas_id: `${PREFIX}layout-brief`, title: 'Layout — brief', template: 'brief', blocks: [
      { id: 'h', slot: 'header', kind: 'markdown', payload: { markdown: '## Daily brief — Monday' } },
      { id: 'k', slot: 'key-points', kind: 'markdown', payload: { markdown: '- Ship the release\n- Call Acme\n- Review 3 PRs\n- ' + LONG_URL } },
      { id: 'b', slot: 'body', kind: 'markdown', payload: { markdown: PARAGRAPH_4000.slice(0, 1200) } },
    ],
  },
  {
    canvas_id: `${PREFIX}layout-status`, title: 'Layout — status board', template: 'status-board', blocks: [
      { id: 'h', slot: 'header', kind: 'html', payload: { html: '<div class="ck-section"><h2 class="ck-section-title">Fleet status</h2><span class="ck-chip ck-success">all green</span></div>' } },
      { id: 's1', slot: 'status', kind: 'kpi', payload: { tiles: [{ label: 'Agents', value: 12 }, { label: 'Running', value: 9 }, { label: 'Errors', value: 0 }] } },
      { id: 's2', slot: 'status', kind: 'html', payload: { html: '<div class="ck-kpi"><p class="ck-kpi-label">Queue</p><p class="ck-kpi-value">3<span class="ck-kpi-unit">pending</span></p></div>' } },
      { id: 'i', slot: 'issues', kind: 'table', title: 'Issues', payload: { columns: ['#', 'Title', 'Sev'], rows: [[1, 'Scheduler drift', 'P2'], [2, 'Slack retries', 'P3']] } },
      { id: 'n', slot: 'next', kind: 'markdown', title: 'Next', payload: { markdown: '1. Rotate keys\n2. Upgrade base image' } },
      { id: 'l', slot: 'log', kind: 'timeline', title: 'Log', payload: { events: [{ ts: '09:00', label: 'Boot' }, { ts: '09:05', label: 'Healthy' }] } },
    ],
  },
  {
    canvas_id: `${PREFIX}layout-sparse`, title: 'Layout — template with nothing slotted', template: 'dashboard', blocks: [
      { id: 'a', kind: 'markdown', payload: { markdown: 'No block names a slot, so this renders stacked.' } },
      { id: 'b', kind: 'kpi', payload: { tiles: [{ label: 'x', value: 1 }] } },
    ],
  },
]

export const GALLERY_IDS = GALLERY.map((c) => c.canvas_id)

// ---------------------------------------------------------------- seeding

/** Log in as admin and return a bearer token (form-encoded, like the CLI). */
export async function adminToken(request, baseURL) {
  const password = process.env.ADMIN_PASSWORD
  if (!password) throw new Error('ADMIN_PASSWORD env var must be set to seed the canvas gallery')
  const res = await request.post(`${baseURL}/api/token`, {
    form: { username: 'admin', password },
  })
  if (!res.ok()) throw new Error(`login failed: ${res.status()} ${await res.text()}`)
  return (await res.json()).access_token
}

/** Write every gallery canvas through the real route. Returns the failures, if any. */
export async function seedGallery(request, baseURL, token, { agent = AGENT, audience = 'roster' } = {}) {
  const failures = []
  for (const c of GALLERY) {
    const { canvas_id, ...body } = c
    const res = await request.put(`${baseURL}/api/agents/${encodeURIComponent(agent)}/canvas/${canvas_id}`, {
      headers: { Authorization: `Bearer ${token}` },
      data: { ...body, audience },
    })
    if (!res.ok()) failures.push({ canvas_id, status: res.status(), body: (await res.text()).slice(0, 300) })
  }
  return failures
}

export async function clearGallery(request, baseURL, token, { agent = AGENT } = {}) {
  for (const id of GALLERY_IDS) {
    await request.delete(`${baseURL}/api/agents/${encodeURIComponent(agent)}/canvas/${id}`, {
      headers: { Authorization: `Bearer ${token}` },
    })
  }
}

export { AGENT as GALLERY_AGENT }

// ---------------------------------------------------------------- page-side helpers

// In-page measurement. Runs after every diagram and image has settled.
export function measureKit() {
  const kit = document.querySelector('[data-testid="canvas-panel"] .canvas-kit')
  if (!kit) return { blocks: 0, issues: [{ msg: 'no canvas kit on the page' }] }
  const kr = kit.getBoundingClientRect()
  const doc = document.documentElement
  const issues = []
  const px = (n) => Math.round(n)
  if (doc.scrollWidth > doc.clientWidth + 1) {
    issues.push({ msg: `page scrolls horizontally: ${doc.scrollWidth} > ${doc.clientWidth}` })
  }
  if (kit.scrollWidth > kit.clientWidth + 1) {
    issues.push({ msg: `kit scrolls horizontally: ${kit.scrollWidth} > ${kit.clientWidth}` })
  }
  const scrolls = (el) => {
    const cs = getComputedStyle(el)
    return /(auto|scroll|hidden|clip)/.test(cs.overflowX) || /(auto|scroll|hidden|clip)/.test(cs.overflowY)
  }
  const blocks = [...kit.querySelectorAll('[data-canvas-block]')]
  for (const b of blocks) {
    const r = b.getBoundingClientRect()
    const id = b.dataset.canvasBlock
    const kind = b.dataset.canvasKind
    if (r.height < 4) issues.push({ id, kind, msg: 'block has no height' })
    // A kind that draws must have drawn SOMETHING — its figure, or its named
    // fallback. A titled block with nothing under the title has height and
    // passes every geometric check while being the worst outcome.
    const drew = {
      diagram: 'svg, pre',
      image: 'img, p',
      chart: 'canvas, svg, .flex.items-end, p, pre',
    }[kind]
    if (drew && !b.querySelector(drew)) issues.push({ id, kind, msg: `${kind} block rendered nothing` })
    if (r.right > kr.right + 1 || r.left < kr.left - 1) {
      issues.push({ id, kind, msg: `block box outside the kit: ${px(r.left)}..${px(r.right)} vs ${px(kr.left)}..${px(kr.right)}` })
    }
    for (const el of b.querySelectorAll('*')) {
      const er = el.getBoundingClientRect()
      if (!er.width || !er.height) continue
      const boxSpill = Math.max(er.right - r.right, r.left - er.left)
      // Text that runs past its own box has no rect of its own; scrollWidth
      // sees it. Only for boxes that do not scroll themselves.
      const textSpill = !scrolls(el) && el.clientWidth ? el.scrollWidth - el.clientWidth : 0
      if (boxSpill <= 1 && textSpill <= 1) continue
      let a = el.parentElement
      let contained = false
      while (a && a !== b) {
        if (scrolls(a)) { contained = true; break }
        a = a.parentElement
      }
      if (contained) continue
      const cls = typeof el.className === 'string' ? el.className.split(/\s+/).slice(0, 3).join(' ') : ''
      const what = boxSpill > 1 ? `spills ${px(boxSpill)}px past the block` : `has ${px(textSpill)}px of text past its box`
      issues.push({ id, kind, msg: `<${el.tagName.toLowerCase()} class="${cls}"> ${what}` })
      break
    }
  }
  // Stacked siblings must not overlap (grid regions lay blocks side by side).
  const byParent = new Map()
  for (const b of blocks) {
    const p = b.parentElement
    if (p.classList.contains('ck-slot-grid')) continue
    if (!byParent.has(p)) byParent.set(p, [])
    byParent.get(p).push(b)
  }
  for (const list of byParent.values()) {
    for (let i = 1; i < list.length; i++) {
      const a = list[i - 1].getBoundingClientRect()
      const c = list[i].getBoundingClientRect()
      if (c.top < a.bottom - 1) {
        issues.push({ id: list[i].dataset.canvasBlock, kind: list[i].dataset.canvasKind, msg: `overlaps the block above by ${px(a.bottom - c.top)}px` })
      }
    }
  }
  return { blocks: blocks.length, issues }
}

// Everything the panel shows asynchronously has arrived: no diagram skeleton,
// no workspace-image skeleton, every <img> decoded (or failed).
export async function settled(page) {
  await page.waitForFunction(() => {
    const kit = document.querySelector('[data-testid="canvas-panel"] .canvas-kit')
    if (!kit) return false
    if (kit.querySelector('[aria-busy="true"]')) return false
    // A lazy image far down a scroll container never loads on its own, so
    // the load-or-fail outcome could not be measured; ask for it eagerly.
    for (const i of kit.querySelectorAll('img[loading="lazy"]')) if (!i.complete) i.loading = 'eager'
    return [...kit.querySelectorAll('img')].every((i) => i.complete)
  }, null, { timeout: 45000 })
}

// For the screenshot only: let the panel's scroll ancestors grow so a full
// page capture shows the whole canvas rather than one viewport of it.
export async function unclip(page) {
  await page.evaluate(() => {
    let el = document.querySelector('[data-testid="canvas-panel"]')
    while (el && el !== document.body) {
      const cs = getComputedStyle(el)
      if (/(auto|scroll|hidden)/.test(cs.overflowY) || cs.height.endsWith('px') && el.scrollHeight > el.clientHeight + 1) {
        el.style.overflow = 'visible'
        el.style.height = 'auto'
        el.style.maxHeight = 'none'
        el.style.minHeight = '0'
      }
      el = el.parentElement
    }
    document.documentElement.style.height = 'auto'
    document.body.style.height = 'auto'
  })
}

