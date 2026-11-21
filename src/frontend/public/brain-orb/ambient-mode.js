/*
 * Brain Orb — ambient mode (trinity-enterprise#1302): a watch-only, hands-off view.
 *
 * Read-only by construction: this module only ever issues GET requests, against
 * routes that already exist and already scope what the viewer may see —
 *   /api/agents/{name}                         status
 *   /api/agents/{name}/executions              runs (a share holder sees only their own)
 *   /api/agents/{name}/executions/{id}/stream  the running run's log (SSE)
 *   /api/agents/{name}/executions/{id}/log     the most recent finished run's log
 *   /api/agents/{name}/queue · /schedules      queued work · next scheduled run
 * What is drawn from those is decided in ambient-core.js (and unit-tested there).
 *
 * Honesty rules: every state on screen is a real signal or says in words that it
 * is not (stopped, feed lost, signed out, "replaying · not live").
 * Longevity: every buffer is capped, every three.js object it creates is disposed.
 */
import {
  STATES, MOODS, MOOD_LEGEND, createNoteMatcher, createClassifier, runName,
  summarizeRuns, deriveMood, titleOf, parseTs, ago, fmt,
} from './ambient-core.js?v=2026-11-19';

const POLL_MS = 15000, SCHEDULES_MS = 300000, GRAPH_MS = 900000;
const IDLE_MS = 40000, REPLAY_MS = 6500, BACKFILL_MS = 1500;
const MAX_ARCS = 40, MAX_WAVES = 12, MAX_LABELS = 9, MAX_USED = 14, MAX_TICKS = 600, MAX_HISTORY = 160, MAX_LOG = 5000;

export function createAmbient(ctx) {
  const THREE = ctx.THREE;
  const $ = (id) => document.getElementById(id);
  const hud = $('ambientHud');
  if (!hud) return null;

  let active = false, snap = null, gen = 0;
  let pollT = null, clockT = null;
  let feed = 'connecting';            // connecting | ok | lost | signed-out | no-runs | standalone
  let fails = 0;
  let agentStatus = null, summary = summarizeRuns([], Date.now()), queued = 0, runsReadable = false;
  let scheduleNames = new Map(), nextRun = null, schedulesAt = 0, graphAt = 0;
  let counts = null;
  let stream = null;                  // {id, abort, seen}
  const seenByRun = new Map();        // run id -> entries already played (reconnect replays the buffer)
  let lastLogId = null, lastLogNotes = false;
  let lastWriteAt = null, lastLiveAt = 0, replayAt = 0, replayIdx = 0;
  let curState = 'resting', orbState = '';
  let matcher = null, matcherFor = null, classifier = null;
  const history = [], ticks = [], usedIds = [];
  const arcs = [], waves = [], labels = [], heated = new Map();
  const V = new THREE.Vector3();
  let unitRing = null;

  /* ------------------------------------------------------------- graph access */
  function notes() {
    const ns = ctx.nodes();
    if (matcherFor !== ns || (matcher && matcher.size !== ns.length)) {
      matcherFor = ns;
      matcher = createNoteMatcher(ns.map((n) => n.id));
      classifier = createClassifier(matcher);
      resetEffects();
    }
    return ns;
  }
  const posOf = (i) => { const n = ctx.nodes()[i]; return n && n._pos ? n._pos : null; };

  /* ------------------------------------------------------------------ effects */
  function ring() {
    if (unitRing) return unitRing;
    const pts = [];
    for (let i = 0; i <= 96; i++) { const a = (i / 96) * Math.PI * 2; pts.push(new THREE.Vector3(Math.cos(a), 0, Math.sin(a))); }
    unitRing = new THREE.BufferGeometry().setFromPoints(pts);
    return unitRing;
  }
  function dropArc(a) { ctx.root.remove(a.line); a.geo.dispose(); a.line.material.dispose(); }
  function dropWave(w) { ctx.scene.remove(w.line); w.line.material.dispose(); }
  function arc(from, to, color, life = 9) {
    const a = from ? from.clone() : new THREE.Vector3(), b = to.clone();
    const mid = a.clone().add(b).multiplyScalar(0.5), d = a.distanceTo(b);
    mid.add(mid.lengthSq() > 1 ? mid.clone().normalize().multiplyScalar(d * 0.3) : new THREE.Vector3(0, d * 0.3, 0));
    const pts = new THREE.QuadraticBezierCurve3(a, mid, b).getPoints(40);
    const geo = new THREE.BufferGeometry().setFromPoints(pts); geo.setDrawRange(0, 0);
    const line = new THREE.Line(geo, new THREE.LineBasicMaterial({ color, transparent: true, opacity: 0.95, blending: THREE.AdditiveBlending, depthWrite: false }));
    ctx.root.add(line); arcs.push({ line, geo, t: 0, life });
    while (arcs.length > MAX_ARCS) dropArc(arcs.shift());
  }
  function shock(color, strength = 1) {
    const line = new THREE.Line(ring(), new THREE.LineBasicMaterial({ color, transparent: true, opacity: 0.5 * strength, blending: THREE.AdditiveBlending, depthWrite: false }));
    line.rotation.set(Math.random() * 3, Math.random() * 3, 0);
    ctx.scene.add(line); waves.push({ line, t: 0, life: 2.6, max: 150 + 60 * strength, o: 0.5 * strength });
    while (waves.length > MAX_WAVES) dropWave(waves.shift());
  }
  function label(i, text, color, life = 11) {
    const hit = labels.find((l) => l.i === i);
    if (hit) { hit.t = 0; hit.life = life; return; }
    const el = document.createElement('div'); el.className = 'am-lbl'; el.textContent = text;
    el.style.borderLeftColor = color; el.style.opacity = '0';
    $('amLabels').appendChild(el); labels.push({ el, i, t: 0, life });
    while (labels.length > MAX_LABELS) labels.shift().el.remove();
  }
  function touch(i) {
    const h = ctx.heat(); if (!h || i >= h.array.length) return;
    h.array[i] = 1; h.needsUpdate = true; heated.set(i, 7);
  }
  function resetEffects() {
    while (arcs.length) dropArc(arcs.pop());
    while (waves.length) dropWave(waves.pop());
    while (labels.length) labels.pop().el.remove();
    const h = ctx.heat(), ns = ctx.nodes();
    if (h) { for (const i of heated.keys()) if (i < h.array.length && ns[i]) h.array[i] = ns[i].heat || 0; h.needsUpdate = true; }
    heated.clear();
  }

  /* --------------------------------------------------------------- status line */
  function setLine(state, what, src) {
    const def = STATES[state] || STATES.working;
    curState = STATES[state] ? state : 'working';
    hud.style.setProperty('--am-state', def[1]);
    $('amVerb').textContent = def[0]; $('amWhat').textContent = what || ''; $('amSrc').textContent = src || '';
    if (def[2] !== orbState) { orbState = def[2]; ctx.setOrbState(orbState); }
  }
  function setPlain(verb, what, src, color = '#55607a') {      // a non-activity status, in words
    curState = 'resting';
    hud.style.setProperty('--am-state', color);
    $('amVerb').textContent = verb; $('amWhat').textContent = what || ''; $('amSrc').textContent = src || '';
    if (orbState !== 'idle') { orbState = 'idle'; ctx.setOrbState('idle'); }
  }

  function usedAdd(id) {
    const k = usedIds.indexOf(id); if (k >= 0) usedIds.splice(k, 1);
    usedIds.unshift(id); if (usedIds.length > MAX_USED) usedIds.length = MAX_USED;
    renderUsed();
  }
  function renderUsed() {
    const list = $('amUsed'); list.textContent = '';
    usedIds.forEach((id, k) => {
      const el = document.createElement('div'); el.className = 'am-n'; el.textContent = titleOf(id);
      el.style.opacity = String(Math.max(0.25, 1 - k / (MAX_USED + 2)));
      list.appendChild(el);
    });
    if (!usedIds.length) {
      const el = document.createElement('div'); el.className = 'am-n am-dim';
      el.textContent = runsReadable ? 'none in the graph yet' : 'unavailable';
      list.appendChild(el);
    }
  }

  /* one activity event -> what you see. soft = replay (dimmer, never changes the live state) */
  function play(ev, soft) {
    const def = STATES[ev.state] || STATES.working, color = def[1];
    const idx = (ev.notes || []).map((id) => ctx.indexOf(id)).filter((i) => i != null && posOf(i));
    if (!soft) setLine(ev.state, ev.label, 'live');
    if (ev.kind === 'recall-notes' && idx.length) {
      idx.slice(0, 12).forEach((i, n) => {
        touch(i); arc(null, posOf(i), color, soft ? 6 : 9);
        if (n < 4) label(i, ctx.nodes()[i].title || titleOf(ev.notes[n]), color, soft ? 6 : 11);
      });
      ctx.focus(idx[0]); shock(color, soft ? 0.5 : 0.8);
    } else if (idx.length) {
      const i = idx[0];
      touch(i); label(i, ctx.nodes()[i].title || titleOf(ev.notes[0]), color, soft ? 7 : 14);
      arc(play.last != null && play.last !== i ? posOf(play.last) : null, posOf(i), color, soft ? 7 : 12);
      ctx.focus(i); play.last = i;
      if (ev.kind === 'write') shock(color, soft ? 0.6 : 1.1);
    } else if (!soft && (ev.kind === 'skill' || ev.kind === 'listen' || ev.kind === 'delegate' || ev.kind === 'web')) {
      shock(color, 0.6);
    }
  }
  function ingest(ev, live) {
    const def = STATES[ev.state] || STATES.working;
    ticks.push({ ts: Date.now(), c: def[1], h: (ev.notes || []).length ? 1 : 0.5 });
    if (ticks.length > MAX_TICKS) ticks.shift();
    if ((ev.notes || []).length) {
      history.push(ev); if (history.length > MAX_HISTORY) history.shift();
      ev.notes.slice(0, 4).forEach((id) => { if (ctx.indexOf(id) != null) usedAdd(id); });
    }
    if (ev.kind === 'write' && ev.notes.length) lastWriteAt = Date.now();
    lastLiveAt = performance.now();
    if (live) play(ev, false); else setLine(ev.state, ev.label, 'live');
  }

  /* ------------------------------------------------------------------ network */
  async function get(path, signal) {
    const r = await fetch(ctx.agentBase() + path, { headers: ctx.headers(), signal: signal || AbortSignal.timeout(12000) });
    if (r.status === 401) { const e = new Error('401'); e.status = 401; throw e; }
    return r;
  }
  function stopStream() { if (stream) { try { stream.abort.abort(); } catch (_) {} stream = null; } }

  async function openStream(id, myGen) {
    stopStream();
    const abort = new AbortController();
    const me = (stream = { id, abort });
    const skip = seenByRun.get(id) || 0;
    let seen = 0, reader = null;
    const t0 = performance.now();
    history.length = 0; usedIds.length = 0; renderUsed(); $('amUsedCap').textContent = 'notes used · this run';
    try {
      const r = await get(`/executions/${encodeURIComponent(id)}/stream`, abort.signal);
      if (!r.ok || !r.body) return;
      reader = r.body.getReader();
      const dec = new TextDecoder();
      let buf = '';
      for (;;) {
        const { value, done } = await reader.read();
        if (done) break;
        buf += dec.decode(value, { stream: true });
        if (buf.length > 4e6) buf = buf.slice(-1e6);           // a line that never ends must not grow forever
        let nl;
        while ((nl = buf.indexOf('\n')) >= 0) {
          const line = buf.slice(0, nl); buf = buf.slice(nl + 1);
          if (!line.startsWith('data:')) continue;
          let entry; try { entry = JSON.parse(line.slice(5)); } catch (_) { continue; }
          if (entry && entry.type === 'stream_end') return;
          if (!active || myGen !== gen || stream !== me) return;
          seen++;
          if (seen <= skip) continue;                         // already played before a reconnect
          seenByRun.set(id, seen);
          const live = performance.now() - t0 > BACKFILL_MS;  // the first burst is the run's past
          for (const ev of classifier.feed(entry)) ingest(ev, live);
        }
      }
    } catch (e) {
      if (e && e.status === 401) feed = 'signed-out';
    } finally {
      if (reader) { try { await reader.cancel(); } catch (_) {} }
      if (stream === me) stream = null;
      if (seenByRun.size > 20) seenByRun.delete(seenByRun.keys().next().value);
    }
  }

  async function loadLastRun(run, myGen) {
    lastLogId = run.id; lastLogNotes = false;
    try {
      const r = await get(`/executions/${encodeURIComponent(run.id)}/log`, AbortSignal.timeout(30000));
      if (!r.ok) return;
      const d = await r.json();
      if (!active || myGen !== gen || stream) return;
      let log = Array.isArray(d.log) ? d.log : [];
      if (log.length > MAX_LOG) log = log.slice(-MAX_LOG);
      const c = createClassifier(matcher);
      history.length = 0; usedIds.length = 0;
      let wrote = false;
      for (const entry of log) for (const ev of c.feed(entry)) {
        if (!ev.notes.length) continue;
        history.push(ev); if (history.length > MAX_HISTORY) history.shift();
        if (ev.kind === 'write') wrote = true;
        ev.notes.slice(0, 4).forEach((id) => { if (ctx.indexOf(id) != null) { const k = usedIds.indexOf(id); if (k >= 0) usedIds.splice(k, 1); usedIds.unshift(id); } });
      }
      usedIds.length = Math.min(usedIds.length, MAX_USED);
      lastLogNotes = true;
      const ended = parseTs(run.completed_at);
      if (wrote && ended) lastWriteAt = Math.max(lastWriteAt || 0, ended);
      $('amUsedCap').textContent = 'notes used · last run';
      renderUsed(); replayIdx = 0;
    } catch (e) { if (e && e.status === 401) feed = 'signed-out'; }
  }

  async function poll() {
    if (!active || document.hidden) return;
    const myGen = gen;
    notes();
    if (!ctx.agentBase()) { feed = 'standalone'; renderAll(); return; }
    try {
      const [a, ex, q] = await Promise.all([get(''), get('/executions?limit=40'), get('/queue')]);
      if (!active || myGen !== gen) return;
      if (!a.ok) throw new Error('agent ' + a.status);
      agentStatus = (await a.json()).status || null;
      runsReadable = ex.ok;
      summary = summarizeRuns(ex.ok ? await ex.json() : [], Date.now());
      queued = q.ok ? ((await q.json()).queue_length || 0) : summary.queuedInList;
      const now = Date.now();
      if (now - schedulesAt > SCHEDULES_MS) {
        schedulesAt = now;
        const s = await get('/schedules');
        if (s.ok) {
          const list = await s.json();
          scheduleNames = new Map(list.map((x) => [x.id, x.name]).filter((x) => x[1]));
          const up = list.filter((x) => x.enabled && parseTs(x.next_run_at) > now).sort((x, y) => parseTs(x.next_run_at) - parseTs(y.next_run_at))[0];
          nextRun = up ? { at: parseTs(up.next_run_at), name: up.name || 'a scheduled task' } : null;
        }
      }
      if (now - graphAt > GRAPH_MS) { graphAt = now; refreshCounts(myGen); }
      fails = 0; feed = runsReadable ? 'ok' : 'no-runs';
      const run = agentStatus === 'running' ? summary.running : null;
      if (run) { if (!stream || stream.id !== run.id) openStream(run.id, myGen); }
      else {
        stopStream();
        if (summary.last && summary.last.id !== lastLogId) loadLastRun(summary.last, myGen);
      }
    } catch (e) {
      if (!active || myGen !== gen) return;
      if (e && e.status === 401) feed = 'signed-out';
      else if (++fails >= 2) feed = 'lost';
      if (feed === 'lost' || feed === 'signed-out') stopStream();
    }
    renderAll();
  }

  async function refreshCounts(myGen) {
    try {
      const r = await get('/brain-orb/data', AbortSignal.timeout(60000));
      if (!r.ok) return;
      const d = await r.json();
      if (active && myGen === gen) { counts = countsOf(d); renderStats(); }
    } catch (_) {}
  }
  function countsOf(d) {
    const m = (d && d.meta) || {}, ns = (d && d.nodes) || [];
    const aged = ns.some((n) => n.age_days != null);
    return {
      notes: m.total_nodes || ns.length, links: m.total_edges || ((d && d.edges) || []).length,
      fresh: aged ? ns.filter((n) => n.age_days != null && n.age_days < 1).length : null,
    };
  }

  /* ---------------------------------------------------------------------- HUD */
  function renderStats() {
    const now = Date.now(), s = summary, c = counts || { notes: null, links: null, fresh: null };
    $('amNotes').textContent = fmt(c.notes); $('amLinks').textContent = fmt(c.links);
    $('amFresh').textContent = c.fresh == null ? '–' : '+' + fmt(c.fresh);
    const up = agentStatus === 'running';
    const known = feed === 'ok' || feed === 'no-runs';
    $('amLed').style.background = !known ? '#55607a' : up ? '#00e0a0' : '#f0b100';
    $('amStatus').textContent = feed === 'standalone' ? 'not connected' : !known ? 'unknown' : !up ? (agentStatus || 'unknown') : s.running ? 'running a task' : 'online · idle';
    const row = (id, v) => { $(id).textContent = v; };
    if (!runsReadable) { row('amRunK', 'runs'); row('amRun', 'not visible to you'); }
    else if (s.running) { row('amRunK', 'running'); row('amRun', `${runName(s.running, scheduleNames)} · ${ago(now - (parseTs(s.running.started_at) || now))}`); }
    else if (s.last) { row('amRunK', 'last run'); row('amRun', `${runName(s.last, scheduleNames)} ${s.last.status === 'failed' ? '✕' : '✓'} ${ago(now - (s.lastEndedAt || now))} ago`); }
    else { row('amRunK', 'last run'); row('amRun', 'none yet'); }
    row('amNext', nextRun && nextRun.at > now ? `${nextRun.name} in ${ago(nextRun.at - now)}` : 'nothing scheduled');
    row('amQueue', known ? fmt(queued) : '–');
    row('amCtx', s.contextPct == null ? '–' : Math.round(s.contextPct * 100) + '%');
    row('amDay', runsReadable ? `${s.runs24h} runs · ${s.failed24h} failed` : '–');
    row('amCost', runsReadable ? '$' + s.cost24h.toFixed(2) : '–');
  }
  function renderMood() {
    const m = deriveMood({
      readable: (feed === 'ok' || feed === 'no-runs') && (runsReadable || agentStatus !== 'running'),
      agentStatus, lastFailed: summary.lastFailed, recentFailures: summary.recentFailures,
      contextPct: summary.contextPct, running: !!summary.running, queued, lastWriteAt,
      lastRunAt: summary.lastRunAt, now: Date.now(),
    });
    $('amMoodWord').textContent = m.word; $('amMoodWord').style.color = m.tone;
    $('amMoodWhy').textContent = m.why;
    hud.style.setProperty('--am-mood', m.tone);
  }
  function renderMissing() {
    const miss = [];
    if (feed === 'standalone') miss.push('no platform connection — graph only');
    if (feed === 'no-runs') miss.push('this agent\'s runs are not visible to you — activity and used notes are hidden');
    if (feed === 'ok' && lastLogNotes && !usedIds.length && !stream) miss.push('the last run touched no notes in this graph');
    $('amMissing').textContent = miss.join(' · ');
  }
  function renderIdleLine() {
    if (feed === 'signed-out') return setPlain('signed out', 'reload the page to sign in again', 'not live', '#f0b100');
    if (feed === 'lost') return setPlain('live feed lost', 'retrying — nothing below is current', 'not live', '#f0b100');
    if (feed === 'standalone') return setPlain('not connected', 'showing the graph only', '');
    if (feed === 'connecting') return setPlain('connecting', '', '');
    if (agentStatus !== 'running') return setPlain('stopped', 'the agent is not running', agentStatus || '');
    if (stream && performance.now() - lastLiveAt < IDLE_MS) return;       // a live line is showing
    if (summary.running) return setLine('working', runName(summary.running, scheduleNames), 'live · waiting for the next step');
    const now = Date.now();
    const lastTxt = summary.last ? `last worked ${ago(now - (summary.lastEndedAt || now))} ago` : runsReadable ? 'no runs yet' : '';
    const nextTxt = nextRun && nextRun.at > now ? `next: ${nextRun.name} in ${ago(nextRun.at - now)}` : 'nothing scheduled';
    setLine('resting', lastTxt, nextTxt);
  }
  function renderRibbon() {
    const now = Date.now(), span = 1800e3, box = $('amTicks'); box.textContent = '';
    for (const t of ticks) {
      const x = 1 - (now - t.ts) / span; if (x < 0 || x > 1) continue;
      const i = document.createElement('i');
      i.style.left = (x * 100).toFixed(2) + '%'; i.style.height = t.h * 100 + '%'; i.style.background = t.c;
      box.appendChild(i);
    }
  }
  function renderClock() {
    const d = new Date();
    $('amClock').textContent = d.toLocaleTimeString('en-GB', { hour: '2-digit', minute: '2-digit' });
    $('amDate').textContent = d.toLocaleDateString('en-GB', { weekday: 'short', day: 'numeric', month: 'short' });
  }
  function renderAll() { if (!active) return; if (!usedIds.length) renderUsed(); renderStats(); renderMood(); renderMissing(); renderIdleLine(); renderRibbon(); renderClock(); }

  /* rest, and replay the last run's path through the notes — always labelled as not live */
  function replay(nowMs) {
    if (feed !== 'ok' || agentStatus !== 'running' || stream || summary.running || !history.length) return;
    if (nowMs - lastLiveAt < IDLE_MS || nowMs - replayAt < REPLAY_MS) return;
    replayAt = nowMs;
    const ev = history[replayIdx++ % history.length];
    const when = summary.lastEndedAt ? ` from ${ago(Date.now() - summary.lastEndedAt)} ago` : '';
    hud.style.setProperty('--am-state', STATES.replaying[1]);
    $('amVerb').textContent = STATES.replaying[0];
    $('amWhat').textContent = ev.notes.length ? titleOf(ev.notes[0]) : '';
    $('amSrc').textContent = `replaying the last run${when} · not live`;
    play(ev, true);
  }

  /* ----------------------------------------------------------- frame update */
  function update(dt) {
    if (!active) return;
    const nowMs = performance.now();
    replay(nowMs);
    for (let k = arcs.length - 1; k >= 0; k--) {
      const a = arcs[k]; a.t += dt;
      a.geo.setDrawRange(0, Math.floor(Math.min(1, a.t / 0.9) * 41));
      a.line.material.opacity = 0.95 * Math.max(0, 1 - Math.pow(a.t / a.life, 2));
      if (a.t > a.life) { dropArc(a); arcs.splice(k, 1); }
    }
    for (let k = waves.length - 1; k >= 0; k--) {
      const w = waves[k]; w.t += dt; const p = w.t / w.life;
      w.line.scale.setScalar(12 + (w.max - 12) * (1 - Math.pow(1 - Math.min(1, p), 3)));
      w.line.material.opacity = w.o * Math.max(0, 1 - p);
      if (p >= 1) { dropWave(w); waves.splice(k, 1); }
    }
    if (heated.size) {
      const h = ctx.heat(), ns = ctx.nodes();
      for (const [i, left] of heated) {
        if (left - dt > 0) { heated.set(i, left - dt); continue; }
        heated.delete(i);
        if (h && i < h.array.length && ns[i]) { h.array[i] = ns[i].heat || 0; h.needsUpdate = true; }
      }
    }
    if (labels.length) {
      ctx.root.updateMatrixWorld();
      for (let k = labels.length - 1; k >= 0; k--) {
        const l = labels[k]; l.t += dt; const p = posOf(l.i);
        if (!p || l.t > l.life) { l.el.remove(); labels.splice(k, 1); continue; }
        V.copy(p).applyMatrix4(ctx.root.matrixWorld); const front = V.z > -20; V.project(ctx.camera);
        l.el.style.left = (V.x * 0.5 + 0.5) * innerWidth + 'px'; l.el.style.top = (-V.y * 0.5 + 0.5) * innerHeight + 'px';
        l.el.style.opacity = String((l.t < l.life - 1.5 ? 1 : 0) * (front ? 1 : 0.18));
      }
    }
  }

  /* -------------------------------------------------------------- enter/exit */
  function onVisible() { if (active && !document.hidden) poll(); }
  function enter() {
    if (active) return;
    active = true; gen++;
    snap = ctx.snapshot();
    feed = ctx.agentBase() ? 'connecting' : 'standalone';
    fails = 0; schedulesAt = 0; graphAt = Date.now(); lastLogId = null; lastLogNotes = false; lastLiveAt = 0; orbState = '';
    history.length = 0; usedIds.length = 0; ticks.length = 0; play.last = null;
    counts = countsOf(ctx.data());
    notes();
    $('amAgent').textContent = ctx.agentName() || 'brain orb';
    $('amMood').title = MOOD_LEGEND;
    $('amUsedCap').textContent = 'notes used';
    document.body.classList.add('ambient-on');
    hud.setAttribute('aria-hidden', 'false');
    renderUsed(); renderAll();
    poll();
    pollT = setInterval(poll, POLL_MS);
    clockT = setInterval(renderAll, 5000);
    document.addEventListener('visibilitychange', onVisible);
  }
  function exit() {
    if (!active) return;
    active = false; gen++;
    clearInterval(pollT); clearInterval(clockT); pollT = clockT = null;
    document.removeEventListener('visibilitychange', onVisible);
    stopStream(); resetEffects();
    document.body.classList.remove('ambient-on');
    hud.setAttribute('aria-hidden', 'true');
    ctx.restore(snap); snap = null;
  }

  return {
    enter, exit, update,
    toggle: () => (active ? exit() : enter()),
    get active() { return active; },
    debug: () => ({ active, feed, agentStatus, state: curState, arcs: arcs.length, waves: waves.length, labels: labels.length, heated: heated.size, history: history.length, ticks: ticks.length, used: usedIds.length, stream: stream ? stream.id : null, moods: Object.keys(MOODS).length }),
  };
}
