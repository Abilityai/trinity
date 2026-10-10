// #3458 — the wrapping contract every Workspace message bubble carries.
//
// A long unbroken token (a URL, a hash, a base64 blob) has no break
// opportunity, so its min-content width is its whole length. The user bubble is
// a fit-content box, and fit-content never goes below min-content: one
// 3000-character token drew a bubble ~27,900px wide, clipped to one unreadable
// line off the left edge of the column.
//
// `anywhere`, deliberately not Tailwind's `break-words` (`overflow-wrap:
// break-word`): the two break the same lines, but only `anywhere` also lowers
// the box's min-content, and min-content is what was sizing the bubble.
// Measured in Chrome on the same markup: 22,751px before, 22,751px with
// `break-word`, 360px with `anywhere`.
//
// Inherited, so it reaches the rendered markdown of an agent bubble too.
// `PortalMarkdown` already resets it to `normal` in table cells (a table
// scrolls in its own box) and already wraps `pre` the same way.
export const BUBBLE_WRAP_CLASS = '[overflow-wrap:anywhere]'
