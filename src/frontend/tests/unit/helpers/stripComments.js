/**
 * Strip comments from source before a structure guard scans it.
 *
 * Several specs here assert things about `Portal.vue` and friends by reading the
 * file and matching against it — the established idiom for the parts no unit
 * test can reach, since this project has no component-mount harness. Each has to
 * strip comments first: a comment explaining what NOT to write necessarily
 * contains the offending string, so an unstripped scan flags its own
 * documentation.
 *
 * Extracted here (#2161) because it was about to gain a third copy, and the
 * copies carried a real defect.
 *
 * **HTML comments are removed by scanning, not by one regex pass.** The obvious
 * `/<!--[\s\S]*?-->/g` is non-greedy, so it consumes to the FIRST `-->` and can
 * leave an opener behind. Looping that regex to a fixpoint does NOT fix it
 * either — `<!<!---->-->` reduces to `<!-->`, which still contains `<!--` and no
 * longer matches, so the loop stops with residue. (Verified; that is why this is
 * an index scan and not the two-line "loop it" patch it looks like it should be.
 * CodeQL flags the regex form as `js/incomplete-multi-character-sanitization`.)
 *
 * Residue matters here even though nothing is rendered: leftover comment text is
 * scanned as if it were code, so a guard fails on the prose that describes the
 * rule it enforces — a confusing false failure, which is exactly what stripping
 * exists to prevent.
 *
 * The scan below terminates and is complete. Each pass is non-increasing in
 * length, and any surviving `<!--` forces another removal (to its `-->`, or to
 * end-of-input when unterminated), so the string must change; at the fixpoint no
 * `<!--` can remain. The outer loop also covers openers formed *across* a
 * removal, e.g. `<!` + comment + `--`.
 *
 * Line comments are stripped last and once: they end at the line break rather
 * than at a marker that can nest, so they leave nothing to re-scan.
 */

function stripHtmlCommentsOnce(code) {
  let out = ''
  let i = 0
  for (;;) {
    const open = code.indexOf('<!--', i)
    if (open === -1) return out + code.slice(i)
    out += code.slice(i, open)
    const close = code.indexOf('-->', open + 4)
    if (close === -1) return out // unterminated: drop the rest
    i = close + 3
  }
}

export function stripComments(code) {
  let out = String(code ?? '')
  let previous
  do {
    previous = out
    out = stripHtmlCommentsOnce(out).replace(/\/\*[\s\S]*?\*\//g, '')
  } while (out !== previous)
  return out
    .replace(/^[ \t]*\/\/.*$/gm, '') // whole-line // comments
    .replace(/([^:])\/\/.*$/gm, '$1') // trailing // comments (keep URLs)
}
