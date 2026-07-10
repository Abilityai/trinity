/*
 * Brain Orb voice tile — client-held Gemini Live (#58 Phase 3, trinity-enterprise#60).
 *
 * Adapted from the standalone Cornelius voice page, hardened for Trinity:
 *   - NO hardcoded API key. The browser authenticates its Gemini Live socket with
 *     a short-lived EPHEMERAL token minted by the Trinity backend. We never see the
 *     platform Gemini key, and never see the user's JWT — the PARENT orb page holds
 *     the JWT and mints the token on our behalf (relayed over postMessage).
 *   - NO tool declarations here. The whole Live config (model, voice, system prompt,
 *     tool surface) is LOCKED into the ephemeral token server-side; the browser only
 *     sends {setup:{model}} (matches the google-genai SDK's Constrained path). This
 *     is also what keeps the deferred Phase-4 WRITE tools off — the browser cannot
 *     widen the tool surface.
 *   - NO p5 CDN visualiser (CSP script-src 'self'), NO localhost tool proxy, NO
 *     transcript logging (Phase 4). Every tool call is forwarded to the parent orb,
 *     which runs it locally (ORB_TOOLS) and drives the visualisation.
 *
 * Ephemeral-token wire format (verified against the SDK's live.py Constrained path):
 *   wss://.../v1alpha.GenerativeService.BidiGenerateContentConstrained?access_token=<token>
 *   The SDK sets `Authorization: Token <name>` as a header; a browser WebSocket
 *   cannot set headers, so the token rides as the `access_token` query param. The
 *   exact live handshake is the one part only verifiable against the live API.
 */
(function () {
  'use strict';

  var WSS_BASE = 'wss://generativelanguage.googleapis.com/ws/google.ai.generativelanguage.v1alpha.GenerativeService.BidiGenerateContentConstrained';
  var MIC_RATE = 16000;
  var OUT_RATE = 24000;
  var TOKEN_TIMEOUT_MS = 10000;   // how long we wait for the parent to relay a token
  var ORB_TOOL_TIMEOUT_MS = 8000; // scope re-export can be slow; visual tools are instant

  // ── state ──────────────────────────────────────────────────────────────────
  var appState = 'IDLE';
  var ws = null;
  var wsClosedByUs = false;
  var muteOutput = false;
  var model = null;               // supplied by the parent along with the token

  var audioCtx = null, micStream = null, micNode = null, nextPlayTime = 0;
  var outAnalyser = null, outDataArray = null;   // feeds the p5 audio-reactive orb

  // #66 transcript capture — mirror the original Cornelius voice proxy: buffer
  // per-turn transcription, collect conversation events, flush them to the parent
  // orb on session end (the parent holds the JWT and POSTs capture_transcript).
  var _events = [];               // {event, ts, text|tool|args} for the conversation
  var _userTextBuf = '', _modelTextBuf = '';
  var _sessionId = null;          // per-session id → Idempotency-Key (double-end = one save)
  var _flushed = false;

  function $(id) { return document.getElementById(id); }

  function _uuid() {
    if (crypto.randomUUID) return crypto.randomUUID();
    // Non-secure-context fallback stays CSPRNG (never Math.random) — the id
    // becomes the capture_transcript Idempotency-Key, so it must be unguessable.
    var b = new Uint8Array(16);
    crypto.getRandomValues(b);
    b[6] = (b[6] & 0x0f) | 0x40; b[8] = (b[8] & 0x3f) | 0x80;
    var h = Array.prototype.map.call(b, function (x) { return ('0' + x.toString(16)).slice(-2); }).join('');
    return h.slice(0, 8) + '-' + h.slice(8, 12) + '-' + h.slice(12, 16) + '-' + h.slice(16, 20) + '-' + h.slice(20);
  }
  function _logEvent(e) { _events.push({ ts: new Date().toISOString(), ...e }); }
  function _flushTurns() {   // push any buffered transcription as turn events
    if (_userTextBuf.trim()) { _logEvent({ event: 'user_turn', text: _userTextBuf.trim() }); _userTextBuf = ''; }
    if (_modelTextBuf.trim()) { _logEvent({ event: 'model_turn', text: _modelTextBuf.trim() }); _modelTextBuf = ''; }
  }
  // The correct flush seam (#66): user-stop goes through endConversation (sets
  // wsClosedByUs), so onclose early-returns — a flush wired only to onclose would
  // never fire on the normal path. Called from BOTH endConversation and the
  // error-close branch; idempotent via _flushed.
  function flushTranscript() {
    if (_flushed) return;
    _flushed = true;
    if (!_sessionId) return;   // never connected — nothing to report
    _flushTurns();
    _logEvent({ event: 'session_end' });
    // #102 — ALWAYS relay, even with no dialogue: the parent orb decides what to do
    // and surfaces the outcome (saved / no dialogue / failed), so a session end is
    // never silently indistinguishable from a broken transcript pipeline.
    try {
      // Origin-pinned: the transcript is sensitive; both frames are same-origin.
      window.parent.postMessage({ type: 'orb-capture-transcript', session_id: _sessionId, events: _events }, window.location.origin);
    } catch (e) { console.warn('[brain-orb voice] transcript relay failed:', e.message); }
  }

  // ── parent bridge (the orb page holds the JWT + runs the visual tools) ───────
  // The parent replies to a token request with {type:'orb-voice-token', ...}. We
  // key each request so a stale reply can't resolve a newer request.
  var _tokenWaiter = null;
  function requestToken() {
    return new Promise(function (resolve, reject) {
      if (window.parent === window) { reject(new Error('not embedded in the orb')); return; }
      var settled = false;
      var timer = setTimeout(function () {
        if (settled) return; settled = true; _tokenWaiter = null;
        reject(new Error('voice token timed out'));
      }, TOKEN_TIMEOUT_MS);
      _tokenWaiter = function (msg) {
        if (settled) return; settled = true; clearTimeout(timer); _tokenWaiter = null;
        if (msg && msg.ok && msg.token) resolve(msg);
        else reject(new Error((msg && msg.error) || 'could not start voice'));
      };
      try { window.parent.postMessage({ type: 'orb-voice-token-request' }, window.location.origin); }
      catch (e) { clearTimeout(timer); _tokenWaiter = null; reject(e); }
    });
  }

  // Forward a Gemini tool call to the parent orb, which runs it against ORB_TOOLS
  // and returns the result (drives the visualisation locally — no server hop).
  function callParentOrb(name, args) {
    return new Promise(function (resolve) {
      if (window.parent === window) { resolve({ error: 'not embedded in the orb' }); return; }
      var id = 'g' + Math.random().toString(36).slice(2);
      var done = false;
      var h = function (ev) {
        if (ev.data && ev.data.type === 'orb-tool-result' && ev.data.id === id) {
          if (done) return; done = true;
          window.removeEventListener('message', h); resolve(ev.data.output);
        }
      };
      window.addEventListener('message', h);
      window.parent.postMessage({ type: 'orb-tool', id: id, name: name, args: args }, window.location.origin);
      setTimeout(function () {
        if (done) return; done = true;
        window.removeEventListener('message', h); resolve({ error: 'orb did not respond' });
      }, ORB_TOOL_TIMEOUT_MS);
    });
  }

  window.addEventListener('message', function (ev) {
    // Same-origin only: the token relay carries the ephemeral credential, so never
    // trust a message from another origin (defense-in-depth atop frame-ancestors 'self').
    if (ev.origin !== window.location.origin) return;
    var m = ev.data;
    if (!m || typeof m !== 'object') return;
    // Parent (orb.js) tells us to fully disconnect (voice tile toggled off with V).
    if (m.type === 'orb-voice-stop') { try { endConversation(); } catch (e) {} return; }
    // Parent re-opened the tile → (re)start the conversation automatically.
    if (m.type === 'orb-voice-start') { if (appState === 'IDLE' || appState === 'ERROR') startConversation(); return; }
    // Token relay reply.
    if (m.type === 'orb-voice-token' && _tokenWaiter) { _tokenWaiter(m); return; }
  });

  // ── UI ───────────────────────────────────────────────────────────────────────
  function setState(s) {
    appState = s;
    var dot = $('state-dot'), txt = $('status-text');
    var startBtn = $('start-btn'), endBtn = $('end-btn'), muteBtn = $('mute-btn');
    dot.className = s.toLowerCase();
    switch (s) {
      case 'IDLE':
        txt.textContent = 'press start to speak';
        startBtn.style.display = 'inline-block'; startBtn.disabled = false;
        endBtn.style.display = 'none'; muteBtn.style.display = 'none';
        break;
      case 'CONNECTING':
        txt.textContent = 'connecting…';
        startBtn.style.display = 'none';
        endBtn.style.display = 'inline-block'; muteBtn.style.display = 'inline-block';
        break;
      case 'READY':
        txt.textContent = muteOutput ? 'listening — replies muted' : 'listening — speak freely';
        startBtn.style.display = 'none';
        endBtn.style.display = 'inline-block'; muteBtn.style.display = 'inline-block';
        break;
      case 'SPEAKING':
        txt.textContent = muteOutput ? 'replying (muted)…' : 'speaking…';
        break;
      case 'ERROR':
        startBtn.style.display = 'inline-block'; startBtn.disabled = false;
        endBtn.style.display = 'none'; muteBtn.style.display = 'none';
        break;
    }
  }

  function setError(msg) {
    console.warn('[brain-orb voice]', msg);
    $('status-text').textContent = msg;
    appState = 'ERROR';
    $('state-dot').className = 'error';
    $('start-btn').style.display = 'inline-block'; $('start-btn').disabled = false;
    $('end-btn').style.display = 'none'; $('mute-btn').style.display = 'none';
  }

  function toggleMute() {
    muteOutput = !muteOutput;
    var btn = $('mute-btn');
    btn.classList.toggle('active', muteOutput);
    btn.textContent = muteOutput ? 'Resume Replies' : "Don't Interrupt";
    if (audioCtx) nextPlayTime = audioCtx.currentTime;   // don't replay queued audio
    setState(appState);
  }

  // ── conversation lifecycle ─────────────────────────────────────────────────
  async function startConversation() {
    setState('CONNECTING');
    wsClosedByUs = false; nextPlayTime = 0;

    var tokenInfo;
    try {
      tokenInfo = await requestToken();   // parent mints via the JWT-gated broker
    } catch (e) { setError('voice unavailable: ' + (e.message || e)); return; }
    model = tokenInfo.model;

    try {
      micStream = await navigator.mediaDevices.getUserMedia({
        audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true }
      });
    } catch (e) {
      cleanupAudio();
      setError('allow microphone access to talk');
      return;
    }

    try {
      audioCtx = new (window.AudioContext || window.webkitAudioContext)();
      if (audioCtx.state === 'suspended') await audioCtx.resume();
      var micSource = audioCtx.createMediaStreamSource(micStream);
      micNode = await setupMicCapture(micSource);

      // Output analyser drives the p5 orb: Cornelius's spoken audio flows through
      // it (enqueueAudio connects each buffer to outAnalyser), so the orb pulses
      // with his speech.
      outAnalyser = audioCtx.createAnalyser();
      outAnalyser.fftSize = 256;
      outAnalyser.smoothingTimeConstant = 0.4;
      outDataArray = new Uint8Array(outAnalyser.frequencyBinCount);
      outAnalyser.connect(audioCtx.destination);

      // access_token in the URL is the ephemeral (single-use, model-locked, short-
      // TTL) token — safe to expose to the browser by design; the JWT never is.
      ws = new WebSocket(WSS_BASE + '?access_token=' + encodeURIComponent(tokenInfo.token));

      ws.onopen = function () {
        // Config is locked in the token; send only the model (SDK Constrained path).
        ws.send(JSON.stringify({ setup: { model: model } }));
        // Start a fresh transcript session (#66).
        _events = []; _userTextBuf = ''; _modelTextBuf = ''; _flushed = false;
        _sessionId = _uuid();
        _logEvent({ event: 'session_start' });
        setTimeout(function () {
          if (appState === 'CONNECTING') {
            setError('timed out connecting to voice');
            wsClosedByUs = true; try { ws && ws.close(); } catch (e) {} ws = null;
            cleanupAudio();
          }
        }, 8000);
      };
      ws.onmessage = async function (event) {
        try {
          var text;
          if (typeof event.data === 'string') text = event.data;
          else if (event.data instanceof ArrayBuffer) text = new TextDecoder().decode(event.data);
          else if (event.data instanceof Blob) text = await event.data.text();
          else return;
          await handleServerMessage(JSON.parse(text));
        } catch (e) { console.warn('[brain-orb voice] parse error:', e.message); }
      };
      ws.onerror = function () { console.warn('[brain-orb voice] WS error (close follows)'); };
      ws.onclose = function (event) {
        if (wsClosedByUs) return;
        flushTranscript();   // #66 — server/error close: still save what we captured
        cleanupAudio();
        if (event.code === 1000 || event.code === 1001) setState('IDLE');
        else setError('voice disconnected (' + event.code + ') — press start to retry');
      };
    } catch (err) {
      cleanupAudio();
      setError('voice error: ' + (err.message || err));
    }
  }

  async function handleServerMessage(data) {
    if (data.setupComplete !== undefined || data.setup_complete !== undefined) {
      setState('READY');
      return;
    }

    var toolCall = data.toolCall || data.tool_call;
    if (toolCall) {
      var calls = toolCall.functionCalls || toolCall.function_calls || [];
      var responses = await Promise.all(calls.map(async function (fc) {
        // Every tool is an orb tool (the locked manifest only declares orb tools);
        // the parent runs it against ORB_TOOLS and drives the visualisation.
        _logEvent({ event: 'tool_call', tool: fc.name, args: fc.args || {} });   // #66 transcript
        var output = await callParentOrb(fc.name, fc.args || {});
        return { id: fc.id, response: { output: output } };
      }));
      if (ws && ws.readyState === WebSocket.OPEN) {
        ws.send(JSON.stringify({ tool_response: { function_responses: responses } }));
      }
      return;
    }

    var content = data.serverContent || data.server_content;
    if (content) {
      var turn = content.modelTurn || content.model_turn;
      if (turn && turn.parts) {
        setState('SPEAKING');
        for (var i = 0; i < turn.parts.length; i++) {
          var blob = turn.parts[i].inlineData || turn.parts[i].inline_data;
          if (blob && blob.data) enqueueAudio(blob.data);
        }
      }
      // #66 transcript — accumulate Gemini's input/output transcription per turn.
      // Model speech arriving flushes any pending user turn first (turn boundary).
      var outTx = content.outputTranscription || content.output_audio_transcription;
      if (outTx && outTx.text) {
        if (_userTextBuf.trim()) { _logEvent({ event: 'user_turn', text: _userTextBuf.trim() }); _userTextBuf = ''; }
        _modelTextBuf += outTx.text;
      }
      var inTx = content.inputTranscription || content.input_audio_transcription;
      if (inTx && inTx.text) _userTextBuf += inTx.text;

      if (content.turnComplete || content.turn_complete ||
          content.generationComplete || content.generation_complete) {
        _flushTurns();   // finalize both buffered turns at the turn boundary
        var wait = Math.max(0, (nextPlayTime - audioCtx.currentTime) * 1000 + 150);
        setTimeout(function () { if (appState === 'SPEAKING') setState('READY'); }, wait);
      }
    }
  }

  function endConversation() {
    flushTranscript();   // #66 — save the transcript BEFORE tearing down (correct seam)
    wsClosedByUs = true;
    if (ws) { try { ws.close(1000, 'user ended'); } catch (e) {} ws = null; }
    cleanupAudio();
    if (muteOutput) {
      muteOutput = false;
      var mb = $('mute-btn'); if (mb) { mb.classList.remove('active'); mb.textContent = "Don't Interrupt"; }
    }
    setState('IDLE');
  }

  // ── audio ──────────────────────────────────────────────────────────────────
  async function setupMicCapture(micSource) {
    try {
      // Same-origin worklet file (not a blob: URL) → passes CSP script-src 'self'.
      await audioCtx.audioWorklet.addModule('./mic-worklet.js');
      var node = new AudioWorkletNode(audioCtx, 'mic-capture');
      micSource.connect(node);
      node.port.onmessage = function (e) { sendAudioChunk(e.data); };
      return node;
    } catch (e) {
      // Fallback for browsers/policies where the worklet won't load.
      console.warn('[brain-orb voice] AudioWorklet failed, using ScriptProcessor:', e.message);
      var sp = audioCtx.createScriptProcessor(4096, 1, 1);
      var sink = audioCtx.createGain(); sink.gain.value = 0;
      micSource.connect(sp); sp.connect(sink); sink.connect(audioCtx.destination);
      sp.onaudioprocess = function (ev) { sendAudioChunk(ev.inputBuffer.getChannelData(0).slice()); };
      return sp;
    }
  }

  function sendAudioChunk(float32Data) {
    if (!ws || ws.readyState !== WebSocket.OPEN || appState === 'CONNECTING') return;
    var down = downsample(float32Data, audioCtx.sampleRate, MIC_RATE);
    var pcm16 = floatToInt16(down);
    ws.send(JSON.stringify({
      realtimeInput: { audio: { mimeType: 'audio/pcm;rate=' + MIC_RATE, data: arrayBufferToBase64(pcm16.buffer) } }
    }));
  }

  function enqueueAudio(base64Data) {
    if (muteOutput) { nextPlayTime = audioCtx.currentTime; return; }
    var binary = atob(base64Data);
    var bytes = new Uint8Array(binary.length);
    for (var i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
    var int16 = new Int16Array(bytes.buffer);
    var f32 = new Float32Array(int16.length);
    for (var j = 0; j < int16.length; j++) f32[j] = int16[j] / 32768.0;
    var buf = audioCtx.createBuffer(1, f32.length, OUT_RATE);
    buf.copyToChannel(f32, 0);
    var src = audioCtx.createBufferSource();
    // Route through the analyser (→ destination) so the p5 orb reacts to the speech.
    src.buffer = buf; src.connect(outAnalyser || audioCtx.destination);
    var at = Math.max(audioCtx.currentTime + 0.01, nextPlayTime);
    src.start(at);
    nextPlayTime = at + buf.duration;
  }

  function cleanupAudio() {
    if (micNode) { try { micNode.disconnect(); } catch (e) {} micNode = null; }
    if (micStream) { micStream.getTracks().forEach(function (t) { t.stop(); }); micStream = null; }
    if (audioCtx) { try { audioCtx.close(); } catch (e) {} audioCtx = null; }
    outAnalyser = null; outDataArray = null;
  }

  function downsample(buf, fromRate, toRate) {
    if (fromRate === toRate) return buf;
    var ratio = fromRate / toRate;
    var out = new Float32Array(Math.round(buf.length / ratio));
    for (var i = 0; i < out.length; i++) {
      var s = Math.floor(i * ratio), e = Math.min(Math.ceil((i + 1) * ratio), buf.length), sum = 0;
      for (var j = s; j < e; j++) sum += buf[j];
      out[i] = sum / (e - s);
    }
    return out;
  }

  function floatToInt16(f32) {
    var i16 = new Int16Array(f32.length);
    for (var i = 0; i < f32.length; i++) {
      var s = Math.max(-1, Math.min(1, f32[i]));
      i16[i] = s < 0 ? s * 0x8000 : s * 0x7FFF;
    }
    return i16;
  }

  function arrayBufferToBase64(buf) {
    var bytes = new Uint8Array(buf), b = '';
    for (var i = 0; i < bytes.byteLength; i++) b += String.fromCharCode(bytes[i]);
    return btoa(b);
  }

  // ── audio-reactive orb (p5) — restored from the original voice tile ──────────
  // The smoke/core sketch pulses with Cornelius's speech: enqueueAudio routes each
  // spoken buffer through `outAnalyser`, which this reads every frame. Vendored p5
  // (script-src 'self'); the CDN load was why this was stripped.
  var audioState = { bass: 0, mid: 0, high: 0, amp: 0 };
  function computeBands(arr) {
    var len = arr.length, bS = 0, mS = 0, hS = 0;
    for (var i = 0; i < len; i++) {
      if (i < len * 0.15) bS += arr[i];
      else if (i < len * 0.50) mS += arr[i];
      else hS += arr[i];
    }
    return { bass: bS/(len*0.15)/255, mid: mS/(len*0.35)/255, high: hS/(len*0.50)/255, amp: (bS+mS+hS)/len/255 };
  }
  function updateAudioState() {
    var out = { bass: 0, mid: 0, high: 0, amp: 0 };
    if (outAnalyser && outDataArray) { outAnalyser.getByteFrequencyData(outDataArray); out = computeBands(outDataArray); }
    audioState.bass = out.bass; audioState.mid = out.mid; audioState.high = out.high; audioState.amp = out.amp;
  }

  var tune = { brightness: 1.0, size: 1.1, speed: 0.8, spread: 1.1, smoke: 220, core: 1.0 };
  function hsbToRgb(h, s, b) {
    h /= 360; s /= 100; b /= 100;
    var r, g, bv, i = Math.floor(h*6), f = h*6 - i;
    var pp = b*(1-s), q = b*(1-f*s), tv = b*(1-(1-f)*s);
    switch (i % 6) { case 0: r=b;g=tv;bv=pp;break; case 1: r=q;g=b;bv=pp;break; case 2: r=pp;g=b;bv=tv;break; case 3: r=pp;g=q;bv=b;break; case 4: r=tv;g=pp;bv=b;break; case 5: r=b;g=pp;bv=q;break; }
    return [Math.round(r*255), Math.round(g*255), Math.round(bv*255)];
  }
  function buildSprites() {
    var cfgs = [{h:205,s:52},{h:210,s:46},{h:212,s:38},{h:202,s:28},{h:198,s:22},{h:208,s:17},{h:200,s:14},{h:206,s:10},{h:210,s:7}];
    return cfgs.map(function (cfg) {
      var sz=128, cx=64, r=61, c=document.createElement('canvas'); c.width=c.height=sz;
      var ctx=c.getContext('2d'), rgb=hsbToRgb(cfg.h,cfg.s,90), rv=rgb[0], gv=rgb[1], bv=rgb[2];
      var grad=ctx.createRadialGradient(cx,cx,0,cx,cx,r);
      grad.addColorStop(0,'rgba('+rv+','+gv+','+bv+',0.94)'); grad.addColorStop(0.32,'rgba('+rv+','+gv+','+bv+',0.52)');
      grad.addColorStop(0.62,'rgba('+rv+','+gv+','+bv+',0.16)'); grad.addColorStop(0.86,'rgba('+rv+','+gv+','+bv+',0.03)');
      grad.addColorStop(1,'rgba('+rv+','+gv+','+bv+',0)');
      ctx.fillStyle=grad; ctx.beginPath(); ctx.arc(cx,cx,r,0,Math.PI*2); ctx.fill();
      return c;
    });
  }
  var sketch = function (p) {
    var particles = [], sprites = buildSprites();
    var coreSize = 45, targetCoreSize = 45, sBass = 0, sMid = 0, sHigh = 0, sAmp = 0;
    function curl(x, y, t, ox, oy) {
      var eps=1.5, sc=0.0035;
      var dy = p.noise(x*sc+ox,(y+eps)*sc+oy,t) - p.noise(x*sc+ox,(y-eps)*sc+oy,t);
      var dx = p.noise((x+eps)*sc+ox,y*sc+oy,t) - p.noise((x-eps)*sc+ox,y*sc+oy,t);
      return { x: dy/(eps*sc*2), y: -dx/(eps*sc*2) };
    }
    function Smoke(idx) {
      this.type = idx % 3;
      this.spriteIdx = this.type*3 + Math.floor(Math.random()*3);
      this.nox = p.random(100); this.noy = p.random(100); this.nosh = p.random(2000);
      this.rot = p.random(p.TWO_PI); this.rotSpd = p.random(-0.012,0.012);
      this.vx = p.random(-0.4,0.4); this.vy = p.random(-0.4,0.4);
      this.reset(true);
    }
    Smoke.prototype.reset = function (init) {
      var a = p.random(p.TWO_PI), r;
      if (this.type===0) r = init ? p.random(18,145) : p.random(15,46);
      else if (this.type===1) r = init ? p.random(44,235) : p.random(38,76);
      else r = init ? p.random(78,315) : p.random(68,112);
      this.x = p.cos(a)*r; this.y = p.sin(a)*r;
      this.life = init ? p.random(0.3,1.0) : 1.0;
      if (this.type===0) { this.baseSize=p.random(32,70); this.aspect=p.random(0.72,1.30); this.decay=p.random(0.0015,0.0040); }
      else if (this.type===1) { this.baseSize=p.random(13,43); this.aspect=p.random(0.26,0.62); this.decay=p.random(0.0013,0.0034); }
      else { this.baseSize=p.random(4,15); this.aspect=p.random(0.16,0.50); this.decay=p.random(0.001,0.0026); }
      this.sz = this.baseSize; this.ia = init ? 1.0 : 0.0;
    };
    Smoke.prototype.update = function (energy, bv, mv, hv) {
      var te = this.type===0 ? bv : (this.type===1 ? mv : hv);
      var t = p.frameCount*0.0022, c = curl(this.x,this.y,t,this.nox,this.noy);
      var cs = 5+energy*12+te*8, dist = p.max(p.sqrt(this.x*this.x+this.y*this.y),1);
      var push = 1.2+energy*3.5+te*2.5;
      this.vx = p.lerp(this.vx, c.x*cs+(this.x/dist)*push, 0.06);
      this.vy = p.lerp(this.vy, c.y*cs+(this.y/dist)*push, 0.06);
      this.x += this.vx*0.5; this.y += this.vy*0.5;
      var ss = this.type===2 ? 3.5 : (this.type===1 ? 1.8 : 0.55);
      this.rotSpd += (p.noise(this.nosh+p.frameCount*0.004)-0.5)*0.0007;
      this.rotSpd = p.constrain(this.rotSpd,-0.042,0.042);
      this.rot += this.rotSpd*ss*(1+te*2.2);
      var spd = p.sqrt(this.vx*this.vx+this.vy*this.vy);
      this.sz = this.baseSize*p.max(0.18,1.2/(1+spd*0.38))*(0.5+te*0.42+energy*0.2);
      this.ia = p.min(1.0,this.ia+0.045);
      this.life -= this.decay;
      if (this.life<=0 || p.sqrt(this.x*this.x+this.y*this.y)>445) this.reset(false);
    };
    Smoke.prototype.display = function () {
      var sp=tune.spread, sz=tune.size, br=tune.brightness;
      var dist=p.sqrt(this.x*this.x+this.y*this.y);
      var fs=this.type===0?95:(this.type===1?135:160), fe=this.type===0?195:(this.type===1?250:285);
      var tF=Math.max(0,Math.min(1,(dist-fs)/(fe-fs)));
      var alpha=this.life*this.ia*(1-tF*tF*(3-2*tF))*(this.type===2?0.22:0.15)*br;
      if (alpha<=0.001) return;
      var w=this.sz*sz*(0.42+this.life*0.58), ctx=p.drawingContext;
      ctx.save(); ctx.translate(this.x*sp,this.y*sp); ctx.rotate(this.rot); ctx.scale(1,this.aspect);
      ctx.globalAlpha=alpha; ctx.drawImage(sprites[this.spriteIdx],-w,-w,w*2,w*2); ctx.restore();
    };
    function drawCore(size) {
      if (tune.core===0) return;
      var c=tune.core, sz=tune.size, ctx=p.drawingContext, R=size*sz;
      ctx.save();
      var glow=ctx.createRadialGradient(0,0,R*0.15,0,0,R*5.8);
      glow.addColorStop(0,'rgba(99,179,237,'+(0.13*c)+')'); glow.addColorStop(0.28,'rgba(70,140,210,'+(0.06*c)+')');
      glow.addColorStop(0.6,'rgba(45,100,175,'+(0.02*c)+')'); glow.addColorStop(1,'rgba(30,70,140,0)');
      ctx.fillStyle=glow; ctx.beginPath(); ctx.arc(0,0,R*5.8,0,Math.PI*2); ctx.fill();
      var t=p.frameCount*0.012*tune.speed;
      var wA=p.noise(t)*0.08+0.96, wB=p.noise(t+50)*0.06+0.97, tilt=p.noise(t*0.4)*Math.PI*0.35;
      var sph=ctx.createRadialGradient(-R*0.26,-R*0.30,0,0,0,R*1.55);
      sph.addColorStop(0,'rgba(235,248,255,'+(0.94*c)+')'); sph.addColorStop(0.10,'rgba(190,228,255,'+(0.88*c)+')');
      sph.addColorStop(0.26,'rgba(130,196,250,'+(0.76*c)+')'); sph.addColorStop(0.42,'rgba(85,155,232,'+(0.54*c)+')');
      sph.addColorStop(0.58,'rgba(52,115,200,'+(0.28*c)+')'); sph.addColorStop(0.72,'rgba(40,92,172,'+(0.10*c)+')');
      sph.addColorStop(0.88,'rgba(30,72,142,'+(0.03*c)+')'); sph.addColorStop(1,'rgba(22,54,112,0)');
      ctx.fillStyle=sph; ctx.beginPath(); ctx.ellipse(0,0,R*wA*1.55,R*wB*1.55,tilt,0,Math.PI*2); ctx.fill();
      var rim=ctx.createRadialGradient(R*0.32,R*0.28,0,R*0.32,R*0.28,R*0.72);
      rim.addColorStop(0,'rgba(120,180,235,'+(0.20*c)+')'); rim.addColorStop(0.5,'rgba(90,150,215,'+(0.07*c)+')'); rim.addColorStop(1,'rgba(60,110,180,0)');
      ctx.fillStyle=rim; ctx.beginPath(); ctx.arc(0,0,R*1.08,0,Math.PI*2); ctx.fill();
      for (var i=0;i<4;i++) {
        var sa=p.noise(p.frameCount*0.006+i*73.1)*Math.PI*2, sr=R*(0.1+p.noise(p.frameCount*0.004+i*41.7)*0.45);
        var sx=Math.cos(sa)*sr-R*0.15, sy=Math.sin(sa)*sr-R*0.18, sA=p.noise(p.frameCount*0.009+i*29.3)*0.18*c;
        var sh=ctx.createRadialGradient(sx,sy,0,sx,sy,R*0.18);
        sh.addColorStop(0,'rgba(235,248,255,'+sA+')'); sh.addColorStop(1,'rgba(200,230,255,0)');
        ctx.fillStyle=sh; ctx.beginPath(); ctx.arc(sx,sy,R*0.18,0,Math.PI*2); ctx.fill();
      }
      ctx.restore();
    }
    p.setup = function () {
      // Cap to 1x device pixels: the smoke/core sketch is fill-bound (many radial
      // gradients/frame). At retina 2x it costs ~4x and starves the main WebGL orb
      // of frames → the big orb jerks. 1x is imperceptible on this small orb and
      // frees the main render loop. (The original ran standalone with no such
      // contention; inside the Trinity SPA + nested iframes it needs the headroom.)
      p.pixelDensity(1);
      p.createCanvas(p.windowWidth, p.windowHeight);
      p.colorMode(p.HSB, 360, 100, 100, 100);
      for (var i=0;i<220;i++) particles.push(new Smoke(i));
    };
    p.draw = function () {
      updateAudioState();
      var dctx = p.drawingContext;
      dctx.globalCompositeOperation = 'destination-out';
      dctx.fillStyle = 'rgba(0,0,0,0.16)'; dctx.fillRect(0,0,p.width,p.height);
      dctx.globalCompositeOperation = 'source-over';
      p.translate(p.width/2, p.height*0.5);
      p.scale(Math.min(p.width,p.height)/540);
      var fr = audioState;
      sBass = p.lerp(sBass, fr.bass, fr.bass>sBass?0.18:0.13);
      sMid  = p.lerp(sMid,  fr.mid,  fr.mid >sMid ?0.18:0.10);
      sHigh = p.lerp(sHigh, fr.high, fr.high>sHigh?0.20:0.10);
      sAmp  = p.lerp(sAmp,  fr.amp,  fr.amp >sAmp ?0.18:0.13);
      var time = p.frameCount*0.007*tune.speed, breathe = (p.sin(time)+1)/2*0.16;
      var aBass=p.max(sBass,breathe*0.9), aMid=p.max(sMid,breathe*0.5), aHigh=p.max(sHigh,breathe*0.25), aAmp=p.max(sAmp,breathe*0.55);
      for (var s of particles) s.update(aAmp,aBass,aMid,aHigh);
      p.blendMode(p.BLEND); for (var s2 of particles) { if (s2.type>0) s2.display(); }
      p.blendMode(p.ADD);   for (var s3 of particles) { if (s3.type===0) s3.display(); }
      p.blendMode(p.BLEND);
      var rawBass=p.max(fr.bass,breathe*0.9), rawAmp=p.max(fr.amp,breathe*0.55);
      targetCoreSize = 33+(rawBass*0.55+aBass*0.45)*36+(rawAmp*0.55+aAmp*0.45)*54;
      coreSize = p.lerp(coreSize, targetCoreSize, targetCoreSize>coreSize?0.15:0.09);
      drawCore(coreSize);
    };
    p.windowResized = function () { p.resizeCanvas(p.windowWidth, p.windowHeight); };
  };

  // ── init ───────────────────────────────────────────────────────────────────
  function init() {
    // Start the audio-reactive orb (idle breathing until audio flows).
    try { if (window.p5) new window.p5(sketch); } catch (e) { console.warn('[brain-orb voice] p5 init failed:', e); }
    $('start-btn').onclick = startConversation;
    $('end-btn').onclick = endConversation;
    $('mute-btn').onclick = toggleMute;
    setState('IDLE');
    // Tell the parent we're loaded (it un-hides / positions the tile as needed).
    if (window.parent !== window) {
      try { window.parent.postMessage({ type: 'orb-voice-ready' }, window.location.origin); } catch (e) {}
      // #60: auto-start on load — opening the tile (via V) should begin the
      // conversation, not require a manual Start click. The V keypress is the
      // user gesture; if the mic is denied it falls to the ERROR state (Start
      // button remains as a retry).
      setTimeout(function () { if (appState === 'IDLE') startConversation(); }, 250);
    }
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
  else init();
})();
