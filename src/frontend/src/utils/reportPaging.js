/**
 * How many rows one page of a tabular report holds.
 *
 * Mirrors the backend's `REPORT_ROWS_PAGE_DEFAULT` (#1537). It lives in a
 * dependency-free leaf, rather than in either store, because BOTH the operator
 * reports store and the Workspace portal store need it (#2162) and neither may
 * import the other: `stores/reports.js` pulls in the shared `api.js` client,
 * which the portal store deliberately does not use, and importing it from there
 * drags that whole graph — plus its axios interceptors — into every spec that
 * touches the portal store.
 *
 * One frontend copy, not two. A page size typed separately in each store drifts,
 * and each store's own tests then pin its own version of the drift.
 */
export const REPORT_ROWS_PAGE = 100
