/**
 * trinity-enterprise#950 — turn an agent's git `remote_url` into what the agent
 * header shows: a short `owner/repo` label and, only when it is safe, a link.
 *
 * `remote_url` is the container's `git remote get-url origin` (userinfo
 * redacted server-side by #2742), so it can be https, scp-style ssh
 * (`git@host:owner/repo.git`), `ssh://`, or a local path. Only an http(s)
 * origin — or an ssh one, rewritten to https on the same host — on a dotted
 * host becomes an href; anything else keeps its label and gets no link, so a `javascript:` or
 * `file:` origin can never become a clickable URL.
 *
 * Returns `{ label, href }` (href may be null), or null when there is nothing
 * to show.
 */
export function repoFromRemote(remoteUrl) {
  const raw = typeof remoteUrl === 'string' ? remoteUrl.trim() : ''
  if (!raw) return null

  let host = null
  let path = null
  let scheme = 'https'

  const scp = raw.match(/^[\w.-]+@([\w.-]+):(?!\/)(.+)$/)
  if (scp) {
    host = scp[1]
    path = scp[2]
  } else {
    let parsed
    try { parsed = new URL(raw) } catch { parsed = null }
    if (parsed && ['http:', 'https:', 'ssh:'].includes(parsed.protocol) && parsed.hostname) {
      host = parsed.host
      path = parsed.pathname
      if (parsed.protocol === 'http:') scheme = 'http'
      // `ssh://git@host:22/…` — the port belongs to ssh, not to the web host.
      if (parsed.protocol === 'ssh:') host = parsed.hostname
    }
  }

  const segments = (path || raw)
    .replace(/\.git\/?$/, '')
    .split('/')
    .filter(Boolean)
  const label = segments.slice(-2).join('/') || raw
  if (!host || !path) return { label, href: null }
  // A dotless host is an ssh-config alias (`git@github-work:…`) or a LAN
  // name: rewritten to https it would be a link to nowhere.
  if (!host.includes('.')) return { label, href: null }

  const cleanPath = path.replace(/\.git\/?$/, '').replace(/^\/+|\/+$/g, '')
  return { label, href: `${scheme}://${host}/${cleanPath}` }
}
