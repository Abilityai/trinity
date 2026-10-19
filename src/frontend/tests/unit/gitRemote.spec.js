/**
 * trinity-enterprise#950 — the agent header names the bound repository and
 * links to it. `remote_url` comes from the agent container (`git remote
 * get-url origin`, userinfo-redacted by #2742), so it is whatever the
 * template's origin says: https, scp-style ssh, or something else entirely.
 * The header must never turn it into a non-http(s) href.
 */
import { describe, it, expect } from 'vitest'
import { repoFromRemote } from '../../src/utils/gitRemote'

describe('repoFromRemote', () => {
  it.each([
    ['https://github.com/acme/agent.git', 'acme/agent', 'https://github.com/acme/agent'],
    ['https://github.com/acme/agent', 'acme/agent', 'https://github.com/acme/agent'],
    ['https://github.com/acme/agent/', 'acme/agent', 'https://github.com/acme/agent'],
    ['http://git.example.com/team/sub/agent.git', 'sub/agent', 'http://git.example.com/team/sub/agent'],
    ['git@github.com:acme/agent.git', 'acme/agent', 'https://github.com/acme/agent'],
    ['ssh://git@github.com/acme/agent.git', 'acme/agent', 'https://github.com/acme/agent'],
    ['  https://github.com/acme/agent.git\n', 'acme/agent', 'https://github.com/acme/agent'],
  ])('%s → %s', (url, label, href) => {
    expect(repoFromRemote(url)).toEqual({ label, href })
  })

  it('a redacted credential never reaches the href or the label', () => {
    // #2742 redacts to `***@` today; the parser must not depend on it.
    const r = repoFromRemote('https://oauth2:secret@github.com/acme/agent.git')
    expect(r.href).toBe('https://github.com/acme/agent')
    expect(JSON.stringify(r)).not.toContain('secret')
  })

  it.each([
    'javascript:alert(1)',
    'data:text/html,hi',
    'file:///home/developer/repo',
    '/home/developer/repo',
  ])('%s gets no link', (url) => {
    const r = repoFromRemote(url)
    expect(r?.href ?? null).toBeNull()
  })

  it.each([
    ['git@github-work:acme/agent.git', 'acme/agent'],
    ['https://gitbox/acme/agent.git', 'acme/agent'],
  ])('%s — a dotless host is an ssh alias or a LAN name, not a web address: named, not linked', (url, label) => {
    expect(repoFromRemote(url)).toEqual({ label, href: null })
  })

  it.each([null, undefined, '', '   '])('%p → null (nothing to show)', (url) => {
    expect(repoFromRemote(url)).toBeNull()
  })
})
