/**
 * What a platform token change means for a `/ws` socket (#3445).
 *
 * The SPA holds two `/ws` connections — the app-wide one (`utils/websocket.js`,
 * followed from `App.vue`) and the Dashboard's (`stores/network.js`, followed
 * from `Dashboard.vue`) — and both obey this one rule. The server fixes a
 * socket's scope when it connects (ent#467), so a socket opened for one session
 * must never carry the next one: a new token reconnects with a fresh ticket, and
 * no token disconnects.
 *
 * A leaf module (no imports) so both owners can use it: `utils/websocket.js`
 * imports `stores/network.js`, so the store cannot import it from there.
 *
 * @param {{socketToken: string|null, token: string|null}} state
 *   `socketToken` — the token the socket was opened for; `token` — the session's now
 * @returns {'connect'|'reconnect'|'disconnect'|'none'}
 */
export function socketActionFor({ socketToken, token }) {
  if (!token) return socketToken ? 'disconnect' : 'none'
  if (!socketToken) return 'connect'
  return socketToken === token ? 'none' : 'reconnect'
}
