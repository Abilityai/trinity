/**
 * An axios instance does not inherit later global-default mutations (#2261).
 *
 * This is the fact the workspace transport boundary rests on. `auth.js` installs
 * the platform JWT as `axios.defaults.headers.common.Authorization` at login —
 * AFTER `stores/clientPortal.js` has created its `portalHttp` instance at module
 * scope. If an instance picked that up, every workspace request would carry the
 * operator's credential whenever the store sent none, which is the #2258
 * disclosure by a different door.
 *
 * Pinned against the REAL axios, not a mock, because it is a property of the
 * library's merge semantics and the only thing that can change it is a version
 * bump. The store's request interceptor is written so the property is not
 * load-bearing (it rebuilds Authorization from the store unconditionally), but a
 * silent change here would still be worth knowing about — a failure means the
 * belt is now the only thing holding, not that the app is broken.
 */
import { describe, it, expect } from 'vitest'
import axios from 'axios'

describe('axios instance isolation', () => {
  it('does not inherit a global default set after the instance was created', async () => {
    const instance = axios.create()
    axios.defaults.headers.common.Authorization = 'Bearer PLATFORM-JWT'

    let sent = null
    instance.defaults.adapter = async (config) => {
      sent = config.headers.Authorization ?? config.headers.authorization ?? null
      return { data: {}, status: 200, statusText: 'OK', headers: {}, config }
    }

    try {
      await instance.get('/api/enterprise/client-portal/my-agents', { headers: {} })
      expect(sent).toBeNull()
    } finally {
      delete axios.defaults.headers.common.Authorization
    }
  })
})
