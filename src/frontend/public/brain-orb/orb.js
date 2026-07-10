/*
 * The Self-Rendering Mind — orb renderer (single inline module; styles live in styles.css).
 * Kept self-contained so the page also runs on file:// (data via data.js, three via CDN importmap).
 *
 * Layout, top → bottom (search the "===" banners to jump):
 *   data load · palette · scene/camera · node shader · membrane · motes · particle pool
 *   build graph from data · domains/legend · tensions · colour modes · highlight helpers
 *   inspector + picking + hover · floating labels · visual state machine · waves
 *   membrane endorse · toast · orientation/fly-to · briefing rail · auto-demo
 *   find-and-jump search · voice tile + orb-control bridge · input · zoom · draggable HUD
 *   main loop · boot
 */
import * as THREE from './vendor/three.module.js';

/* ============================ data load ============================ */
/* Trinity embed handshake (#58): an AgentBrainOrb.vue host posts
   {type:'brain-orb:init', agentName, apiBase, authToken} over same-origin
   postMessage; we reply 'brain-orb:ready' first. Standalone (file://) falls
   back to window.AGENT_DATA / ./data.json so the assets render without a host. */
const TRINITY_EMBED = (() => {
  let resolveInit; const ready = new Promise(r => { resolveInit = r; }); let settled = false;
  const finish = v => { if (!settled) { settled = true; resolveInit(v); } };
  if (window.parent && window.parent !== window) {
    window.addEventListener('message', e => {
      if (e.origin !== window.location.origin) return;        // same-origin only
      const d = e.data; if (!d || d.type !== 'brain-orb:init') return;
      finish({ agentName: d.agentName, apiBase: d.apiBase || '', authToken: d.authToken || '', voiceAvailable: !!d.voiceAvailable, writeAvailable: !!d.writeAvailable });
    });
    try { window.parent.postMessage({ type: 'brain-orb:ready' }, window.location.origin); } catch (_) {}
    setTimeout(() => finish(null), 8000);                     // don't hang if no host answers
  } else { finish(null); }
  return { ready };
})();

// Trinity-brokered control surface (#58 Phase 2): the per-agent proxy base + auth
// header the data/scope fetches use once the embed init arrives, and whether live
// scope control is available. Empty/false standalone (relative-path fallbacks).
let ORB_HEADERS = {};
let SCOPE_ENABLED = false;
let VOICE_AVAILABLE = false;   // #60 Phase 3 — gates the client-held voice tile
let WRITE_AVAILABLE = false;   // #61 Phase 4a — platform flag for the KB-write surface

async function loadData(){
  if (window.AGENT_DATA) return window.AGENT_DATA;          // file:// safe
  const ctx = await TRINITY_EMBED.ready;
  if (ctx && ctx.agentName) {
    // Point every brokered fetch (data + scope control) at the per-agent proxy
    // base and carry the platform JWT — replaces the old localhost voice proxy.
    VOICE_PROXY = (ctx.apiBase || '') + '/api/agents/' + encodeURIComponent(ctx.agentName) + '/brain-orb';
    ORB_HEADERS = ctx.authToken ? { Authorization: 'Bearer ' + ctx.authToken } : {};
    SCOPE_ENABLED = true;
    // #60 Phase 3: un-hide the voice tile only when the host says voice is available
    // (platform flag + agent capability). orb-trinity.css keeps it hidden otherwise.
    if (ctx.voiceAvailable) { VOICE_AVAILABLE = true; try { document.body.classList.add('brain-orb-voice'); } catch(_){} }
    // #61 Phase 4a: the KB-write surface is a platform flag here; initActions() only
    // un-hides the action panel (body.brain-orb-write) after the broker confirms the
    // caller owns the agent AND the agent ships a write hook (GET /actions → 200).
    if (ctx.writeAvailable) { WRITE_AVAILABLE = true; }
    const r = await fetch(VOICE_PROXY + '/data', { headers: ORB_HEADERS });
    if (!r.ok) throw new Error('brain-orb data ' + r.status);
    return await r.json();
  }
  try { const r = await fetch('./data.json'); return await r.json(); }  // standalone dev
  catch(e){ return FALLBACK; }                              // minimal offline fixture
}
const FALLBACK = { meta:{total_nodes:0,total_edges:0,sampled_nodes:8,sampled_edges:0},
  nodes:[...Array(80)].map((_,i)=>({id:'n'+i,title:'note '+i,layer:1+(i%7),
    lifecycle:['reflective','crystallizing','generative'][i%3],
    provenance:['originated','endorsed','encountered','ai-inferred'][i%4],
    heat:Math.abs(Math.sin(i)),stale:0,degree:2+i%6})),
  edges:[...Array(70)].map((_,i)=>({source:'n'+i,target:'n'+((i*7+3)%80),type:'associates',weight:.5})),
  tensions:[{a:'n1',b:'n4',strength:.6,demo:true}],
  incubation:[{topic:'demo-topic',confidence:.5,converged:true,runs:6}] };

/* ============================ palette ============================ */
const COL = {
  void:0x06051d, deepNavy:0x061434,
  originated:new THREE.Color('#aee9ff'),
  endorsed:  new THREE.Color('#63b3ed'),
  encountered:new THREE.Color('#5a6b86'),
  aiInferred:new THREE.Color('#f0b100'),
  reference: new THREE.Color('#a78bfa'),
  tension:   new THREE.Color('#ff2056'),
};
const EDGE_COL = {
  'derives-from':new THREE.Color('#3a5a9a'),
  'instantiates':new THREE.Color('#2f7d6a'),
  'references':  new THREE.Color('#2a3454'),
  'associates':  new THREE.Color('#222b44'),
};
const provColor = p => COL[p==='ai-inferred'?'aiInferred':p] || COL.encountered;
const LAYER_NAME = ['','signal','impression','insight','framework','lens','synthesis','index'];

const R = 120;                  // sphere radius

/* ============================ scene ============================ */
const canvas = document.getElementById('scene');
const renderer = new THREE.WebGLRenderer({canvas, antialias:true, alpha:true});
renderer.setPixelRatio(Math.min(devicePixelRatio,1.5));   // #60: cap retina 2x→1.5x for GPU headroom inside the Trinity SPA (the standalone had the whole thread; here the orb shares it) — halves fill jank on hi-DPR displays
renderer.setSize(innerWidth, innerHeight);

const scene = new THREE.Scene();
scene.fog = new THREE.FogExp2(COL.void, 0.0016);

const camera = new THREE.PerspectiveCamera(52, innerWidth/innerHeight, 1, 4000);
let camDist = 330, camTargetDist = 330;
camera.position.set(0,0,camDist);

const root = new THREE.Group();          // everything that rotates/breathes
scene.add(root);

/* ---- glow sprite texture (built at runtime, no external asset) ---- */
function glowTexture(){
  const s=128, c=document.createElement('canvas'); c.width=c.height=s;
  const g=c.getContext('2d'); const grd=g.createRadialGradient(s/2,s/2,0,s/2,s/2,s/2);
  grd.addColorStop(0,'rgba(255,255,255,1)'); grd.addColorStop(.25,'rgba(255,255,255,.55)');
  grd.addColorStop(1,'rgba(255,255,255,0)'); g.fillStyle=grd; g.fillRect(0,0,s,s);
  const t=new THREE.CanvasTexture(c); return t;
}
const GLOW = glowTexture();

/* ============================ node shader ============================ */
const nodeUniforms = {
  uTime:{value:0}, uPixelRatio:{value:renderer.getPixelRatio()},
  uWave:{value:new THREE.Vector4(0,0,0,0)}, uWaveOn:{value:0}, uWaveType:{value:0}, uWaveWidth:{value:34},
  uTint:{value:new THREE.Color('#63b3ed')}, uTintAmt:{value:0}, uHeatBoost:{value:0},
  uBright:{value:new THREE.Vector4(0.42,0.95,1.0,0.55)},   // lens: (floor, gain, gamma, warm-threshold)
};
const nodeMat = new THREE.ShaderMaterial({
  uniforms:nodeUniforms, transparent:true, depthWrite:false, blending:THREE.AdditiveBlending,
  vertexShader:`
    attribute vec3 aColor; attribute float aSize; attribute float aHeat; attribute float aPhase; attribute float aFresh; attribute float aHover; attribute float aDim;
    uniform float uTime, uPixelRatio, uWaveOn, uWaveType, uWaveWidth, uTintAmt, uHeatBoost;
    uniform vec4 uWave, uBright; uniform vec3 uTint;
    varying vec3 vColor;
    void main(){
      vec3 col = aColor;
      float pulse = 0.6 + 0.4*sin(uTime*0.55 + aPhase*6.2831);
      // brightness mapping is lens-controlled: uBright = (floor, gain, gamma, warm-threshold)
      float heatL = uBright.x + pow(clamp(aHeat,0.0,1.0), uBright.z) * uBright.y;
      col *= heatL * (0.85 + 0.15*pulse);
      col += vec3(0.30,0.10,0.0) * smoothstep(uBright.w,1.0,aHeat) * (0.5+0.5*pulse); // warm = active
      float sizeMul = 1.0;
      // freshly-learned notes twinkle white (recency), in any colour mode
      float spark = 0.5 + 0.5*sin(uTime*1.4 + aPhase*9.0);
      col += vec3(0.9,0.95,1.0) * aFresh * (0.35 + 0.40*spark);
      sizeMul += aFresh*0.6*spark;
      // hover highlight: 2 = the hovered node, 1 = its direct neighbours
      col += vec3(0.55,0.66,0.95) * aHover * 0.5;
      col *= (1.0 + aHover*0.30);
      sizeMul += aHover*0.95;
      if(uWaveOn > 0.5){
        float d = distance(position, uWave.xyz);
        float band = exp(-pow((d - uWave.w)/uWaveWidth, 2.0));
        if(uWaveType < 0.5){                       // staleness: grey contagion
          col = mix(col, vec3(0.34,0.36,0.42), band*0.88);
        } else if(uWaveType < 1.5){                // thinking: aurora front + lateral inhibition
          col = mix(col*0.32, col + vec3(0.5,0.45,1.05)*1.4, band);
          sizeMul += band*0.9;
        } else {                                   // ambient: soft additive light-sweep (screensaver)
          col += vec3(0.30,0.42,0.78) * band * 0.85;
          sizeMul += band*0.40;
        }
      }
      col = mix(col, col*uTint*1.9, uTintAmt*0.45);
      col += uTint * uHeatBoost * 0.5;
      col *= aDim;                                   // focus: suppress nodes below the lens threshold
      vColor = col;
      vec4 mv = modelViewMatrix * vec4(position,1.0);
      sizeMul *= (0.55 + 0.45*aDim);                 // focus-dimmed nodes also shrink a touch
      float baseSize = aSize * (0.55 + aHeat*0.9);
      gl_PointSize = baseSize * sizeMul * uPixelRatio * (300.0 / -mv.z);
      gl_Position = projectionMatrix * mv;
    }`,
  fragmentShader:`
    varying vec3 vColor;
    void main(){
      vec2 uv = gl_PointCoord - 0.5;
      float r = length(uv);
      if(r > 0.5) discard;
      float core = smoothstep(0.5, 0.0, r);
      float glow = pow(core, 1.8);
      gl_FragColor = vec4(vColor*glow, glow);
    }`,
});

/* ============================ membrane (glassy iridescent shell) ============================ */
const membraneUniforms = { uTime:{value:0}, uOpen:{value:0} };
const membrane = new THREE.Mesh(
  new THREE.SphereGeometry(R*1.04, 64, 48),
  new THREE.ShaderMaterial({
    uniforms:membraneUniforms, transparent:true, depthWrite:false,
    side:THREE.BackSide, blending:THREE.AdditiveBlending,
    vertexShader:`
      varying vec3 vN; varying vec3 vV;
      void main(){
        vN = normalize(normalMatrix * normal);
        vec4 mv = modelViewMatrix * vec4(position,1.0);
        vV = normalize(-mv.xyz);
        gl_Position = projectionMatrix * mv;
      }`,
    fragmentShader:`
      uniform float uTime, uOpen; varying vec3 vN; varying vec3 vV;
      void main(){
        float rim = pow(1.0 - abs(dot(normalize(vN), normalize(vV))), 2.4);
        vec3 a = vec3(0.10,0.18,0.42);
        vec3 b = vec3(0.35,0.55,0.95);
        vec3 c = vec3(0.0,0.74,0.49);
        float t = 0.5+0.5*sin(uTime*0.25 + vN.y*3.0);
        vec3 irid = mix(a, mix(b,c,t), rim);
        float alpha = rim*0.30 + uOpen*0.25;
        gl_FragColor = vec4(irid, alpha);
      }`,
  })
);
root.add(membrane);

/* ============================ atmosphere motes ============================ */
function buildMotes(n){
  const pos=new Float32Array(n*3);
  for(let i=0;i<n;i++){
    const r=R*(1.15+Math.random()*1.1), u=Math.random()*2-1, th=Math.random()*Math.PI*2, s=Math.sqrt(1-u*u);
    pos[i*3]=r*s*Math.cos(th); pos[i*3+1]=r*u; pos[i*3+2]=r*s*Math.sin(th);
  }
  const g=new THREE.BufferGeometry(); g.setAttribute('position',new THREE.BufferAttribute(pos,3));
  const m=new THREE.PointsMaterial({size:1.7,map:GLOW,color:0x6f86b5,transparent:true,opacity:.5,
    depthWrite:false,blending:THREE.AdditiveBlending,sizeAttenuation:true});
  return new THREE.Points(g,m);
}
const motes = buildMotes(1300); scene.add(motes);

/* ============================ transient particle pool ============================ */
const POOL=320;
const partPos=new Float32Array(POOL*3);
const partData=[]; // {active,vx,vy,vz,life}
for(let i=0;i<POOL;i++){ partData.push({active:false}); partPos[i*3+1]=99999; }
const partGeo=new THREE.BufferGeometry(); partGeo.setAttribute('position',new THREE.BufferAttribute(partPos,3));
const particles=new THREE.Points(partGeo,new THREE.PointsMaterial({
  size:2.4,map:GLOW,color:0x9fd6ff,transparent:true,opacity:.85,depthWrite:false,blending:THREE.AdditiveBlending}));
root.add(particles);
function spawnParticle(out){
  for(let i=0;i<POOL;i++){ if(!partData[i].active){
    const d=partData[i]; d.active=true; d.life=1;
    const u=Math.random()*2-1, th=Math.random()*Math.PI*2, s=Math.sqrt(1-u*u);
    const dir=new THREE.Vector3(s*Math.cos(th),u,s*Math.sin(th));
    if(out){ // hungry: from surface outward
      const p=dir.clone().multiplyScalar(R*0.9);
      partPos[i*3]=p.x;partPos[i*3+1]=p.y;partPos[i*3+2]=p.z;
      d.vx=dir.x*2.4;d.vy=dir.y*2.4;d.vz=dir.z*2.4;
    } else {  // ingesting: from outside inward
      const p=dir.clone().multiplyScalar(R*2.1);
      partPos[i*3]=p.x;partPos[i*3+1]=p.y;partPos[i*3+2]=p.z;
      d.vx=-dir.x*3.0;d.vy=-dir.y*3.0;d.vz=-dir.z*3.0;
    }
    return;
  }}
}
function updateParticles(dt){
  let any=false;
  for(let i=0;i<POOL;i++){ const d=partData[i]; if(!d.active) continue; any=true;
    partPos[i*3]+=d.vx; partPos[i*3+1]+=d.vy; partPos[i*3+2]+=d.vz;
    d.life-=dt*0.55;
    const r=Math.hypot(partPos[i*3],partPos[i*3+1],partPos[i*3+2]);
    if(d.life<=0 || r<R*0.5 || r>R*2.6){ d.active=false; partPos[i*3+1]=99999; }
  }
  if(any) partGeo.attributes.position.needsUpdate=true;
}

/* ============================ build graph from data ============================ */
let NODES=[], idIndex=new Map(), titleIndex=new Map(), adjacency=new Map(), nodePoints=null;
let incCells=[];  // incubation sprites (ai-inferred quarantined cells)
let DATA=null;

function fib(i,n){ // fibonacci sphere unit vector
  const k=i+0.5, phi=Math.acos(1-2*k/n), th=Math.PI*(1+Math.sqrt(5))*k;
  return new THREE.Vector3(Math.cos(th)*Math.sin(phi), Math.sin(th)*Math.sin(phi), Math.cos(phi));
}
function layerRadius(layer){ return R*(0.40 + 0.60*(1-(layer-1)/6)); } // 1=surface,7=core

// recency palette: fresh = bright, fading to cold steel with age
function recencyColor(age){
  if(age<=2)  return new THREE.Color('#dffaff');
  if(age<=7)  return new THREE.Color('#3cf0b0');
  if(age<=30) return new THREE.Color('#63b3ed');
  if(age<=90) return new THREE.Color('#3f5170');
  return new THREE.Color('#243049');
}

/* ---- KB domains / areas: each node assigned to one by keyword match (title weighted) ---- */
const DOMAINS=[
  {key:'ai',       label:'AI / Agents',       color:'#63b3ed', kw:['agent','llm','a.i','artificial intelligence','model','claude','anthropic','prompt','context window','harness','orchestrat','mcp','tool-use','tool use','autonomous','multi-agent','retrieval','rag','reasoning','coordination','inference','fine-tun','token']},
  {key:'neuro',    label:'Neuro / Dopamine',  color:'#ff7eb6', kw:['dopamine','neuro','brain','reward','motivation','prefrontal','default mode','dmn','cognition','attention','craving','addiction','neurotransmitter','serotonin','amygdala','nervous system']},
  {key:'mind',     label:'Buddhism / Mind',   color:'#a78bfa', kw:['buddhis','consciousness','meditat','mindful','no-self','no self','ego','awareness','emptiness','non-dual','dharma','contemplat','enlighten','selfless','impermanence','flow state','flow is','presence','suffering','duhkha']},
  {key:'decision', label:'Decision-Making',   color:'#f0b100', kw:['decision','bias','heuristic','mental model','forecast','probabilit','uncertainty','risk','judgment','judgement','rational','superforecast','expected value','game theory','tradeoff','trade-off']},
  {key:'identity', label:'Identity / Belief', color:'#00bc7d', kw:['identity','belief','reality tunnel','self-construct','narrative','worldview','meaning','values','ideolog','self-image','reference frame']},
  {key:'business', label:'Business / GTM',    color:'#ffae5c', kw:['business','market','content','brand','gtm','go-to-market','sales','customer','monetis','monetiz','growth','startup','revenue','distribution','pricing','b2b','saas','marketing']},
];
const DOMAIN_OTHER={key:'other', label:'Other', color:'#46566f'};
function assignDomain(nd){
  const t=(nd.title||'').toLowerCase();
  const body=(nd.content||'').slice(0,800).toLowerCase();
  let best=null, bestScore=0;
  for(const d of DOMAINS){
    let s=0;
    for(const k of d.kw){ if(t.includes(k)) s+=3; if(body.includes(k)) s+=1; }
    if(s>bestScore){ bestScore=s; best=d; }
  }
  return best && bestScore>=2 ? best.key : 'other';
}
const domainColor=key=>new THREE.Color((DOMAINS.find(d=>d.key===key)||DOMAIN_OTHER).color);
let provColArr=null, recColArr=null, domColArr=null, domainCounts={}, colorMode='provenance';

function buildGraph(data){
  DATA=data;
  const ns=data.nodes; const n=ns.length;
  const pos=new Float32Array(n*3), col=new Float32Array(n*3),
        size=new Float32Array(n), heat=new Float32Array(n), phase=new Float32Array(n), fresh=new Float32Array(n);
  provColArr=new Float32Array(n*3); recColArr=new Float32Array(n*3); domColArr=new Float32Array(n*3);
  domainCounts={}; DOMAINS.forEach(d=>domainCounts[d.key]=0); domainCounts.other=0;
  const phaseSize={reflective:2.4,crystallizing:3.6,generative:5.6};
  const maxDeg=ns.reduce((m,nd)=>Math.max(m,nd.degree||0),1);   // for the degree size boost
  GMAXDEG=maxDeg; GMAXIN=ns.reduce((m,nd)=>Math.max(m,nd.in_degree||0),1);  // lens normalisation
  tensionSet.clear();
  ns.forEach((nd,i)=>{
    const dir=fib(i,n); const rad=layerRadius(nd.layer);
    const p=dir.multiplyScalar(rad);
    pos[i*3]=p.x;pos[i*3+1]=p.y;pos[i*3+2]=p.z;
    const c=provColor(nd.provenance);
    provColArr[i*3]=c.r;provColArr[i*3+1]=c.g;provColArr[i*3+2]=c.b;
    const rc=recencyColor(nd.age_days==null?999:nd.age_days);
    recColArr[i*3]=rc.r;recColArr[i*3+1]=rc.g;recColArr[i*3+2]=rc.b;
    nd._domain=assignDomain(nd); domainCounts[nd._domain]=(domainCounts[nd._domain]||0)+1;
    const dc=domainColor(nd._domain);
    domColArr[i*3]=dc.r;domColArr[i*3+1]=dc.g;domColArr[i*3+2]=dc.b;
    col[i*3]=c.r;col[i*3+1]=c.g;col[i*3+2]=c.b;
    // base size by lifecycle phase, gently enlarged for well-connected nodes (sqrt so hubs don't dominate)
    size[i]=(phaseSize[nd.lifecycle]||2.6) * (1 + Math.sqrt((nd.degree||0)/maxDeg)*0.85);
    heat[i]=nd.heat||0; phase[i]=Math.random();
    const age=nd.age_days==null?999:nd.age_days;
    fresh[i]=Math.max(0,Math.min(1,1-age/7));   // twinkle fades over a week
    idIndex.set(nd.id,i);
    { const tk=nd.title.toLowerCase(); if(!titleIndex.has(tk)) titleIndex.set(tk,i); }
    NODES.push({...nd, _pos:p});
  });
  const g=new THREE.BufferGeometry();
  g.setAttribute('position',new THREE.BufferAttribute(pos,3));
  g.setAttribute('aColor',new THREE.BufferAttribute(col,3));
  g.setAttribute('aSize',new THREE.BufferAttribute(size,1));
  g.setAttribute('aHeat',new THREE.BufferAttribute(heat,1));
  g.setAttribute('aPhase',new THREE.BufferAttribute(phase,1));
  g.setAttribute('aFresh',new THREE.BufferAttribute(fresh,1));
  g.setAttribute('aHover',new THREE.BufferAttribute(new Float32Array(n),1));
  g.setAttribute('aDim',new THREE.BufferAttribute(new Float32Array(n).fill(1),1));   // lens focus-dim multiplier
  nodePoints=new THREE.Points(g,nodeMat); root.add(nodePoints);

  // highlighted edges drawn on hover (hovered node -> its neighbours)
  hoverLines=new THREE.LineSegments(new THREE.BufferGeometry(),
    new THREE.LineBasicMaterial({color:0xaee9ff,transparent:true,opacity:.72,depthWrite:false,blending:THREE.AdditiveBlending}));
  hoverLines.visible=false; root.add(hoverLines);

  /* ---- edges (built strongest-first so the lens 'edge density' slider can thin
     the web via drawRange without rebuilding geometry) ---- */
  const ep=[],ec=[]; let edrawn=0;
  [...data.edges].sort((x,y)=>(y.weight||0)-(x.weight||0)).forEach(e=>{
    const a=idIndex.get(e.source), b=idIndex.get(e.target);
    if(a==null||b==null) return;
    const pa=NODES[a]._pos, pb=NODES[b]._pos;
    ep.push(pa.x,pa.y,pa.z, pb.x,pb.y,pb.z);
    const c=(EDGE_COL[e.type]||EDGE_COL.associates);
    ec.push(c.r,c.g,c.b, c.r,c.g,c.b); edrawn++;
    if(!adjacency.has(e.source)) adjacency.set(e.source,[]);
    if(!adjacency.has(e.target)) adjacency.set(e.target,[]);
    adjacency.get(e.source).push({id:e.target,type:e.type,weight:e.weight});
    adjacency.get(e.target).push({id:e.source,type:e.type,weight:e.weight});
  });
  const eg=new THREE.BufferGeometry();
  eg.setAttribute('position',new THREE.BufferAttribute(new Float32Array(ep),3));
  eg.setAttribute('color',new THREE.BufferAttribute(new Float32Array(ec),3));
  edgeCount=edrawn;
  edgeLines=new THREE.LineSegments(eg,new THREE.LineBasicMaterial({
    vertexColors:true,transparent:true,opacity:.16,depthWrite:false,blending:THREE.AdditiveBlending}));
  root.add(edgeLines);

  CONVERGED=data.converged||[];   // the agent's full converged conclusions (for the voice's list_converged_topics)

  /* ---- tension seams (red, pulsing, immune to staleness) ---- */
  const tp=[]; TENSIONS=[];
  (data.tensions||[]).forEach(t=>{
    const a=idIndex.get(t.a), b=idIndex.get(t.b); if(a==null||b==null) return;
    const pa=NODES[a]._pos, pb=NODES[b]._pos; tp.push(pa.x,pa.y,pa.z,pb.x,pb.y,pb.z);
    TENSIONS.push({a,b,strength:t.strength,demo:!!t.demo}); tensionSet.add(a); tensionSet.add(b);
  });
  const tg=new THREE.BufferGeometry();
  tg.setAttribute('position',new THREE.BufferAttribute(new Float32Array(tp),3));
  tensionLines=new THREE.LineSegments(tg,new THREE.LineBasicMaterial({
    color:COL.tension,transparent:true,opacity:.6,depthWrite:false,blending:THREE.AdditiveBlending}));
  root.add(tensionLines);
  // bright highlight for a single surfaced tension
  tensionHi=new THREE.LineSegments(new THREE.BufferGeometry(),
    new THREE.LineBasicMaterial({color:0xff5a7d,transparent:true,opacity:.95,depthWrite:false,blending:THREE.AdditiveBlending}));
  tensionHi.visible=false; root.add(tensionHi);
  // connections drawn when a whole topic-cluster is lit up
  clusterLines=new THREE.LineSegments(new THREE.BufferGeometry(),
    new THREE.LineBasicMaterial({color:0x9fd6ff,transparent:true,opacity:.55,depthWrite:false,blending:THREE.AdditiveBlending}));
  clusterLines.visible=false; root.add(clusterLines);

  /* ---- incubation: ai-inferred thought-cells quarantined OUTSIDE the core ---- */
  const inc=data.incubation||[];
  inc.forEach((t,i)=>{
    const dir=fib(i,Math.max(inc.length,8)).multiplyScalar(R*1.55);
    const spr=new THREE.Sprite(new THREE.SpriteMaterial({
      map:GLOW, color:new THREE.Color('#f0b100'), transparent:true, opacity:.9,
      depthWrite:false, blending:THREE.AdditiveBlending}));
    const sz=10+ (t.confidence||.4)*22; spr.scale.set(sz,sz,1);
    spr.position.copy(dir);
    spr.userData={topic:t.topic, confidence:t.confidence, converged:!!t.converged,
                  endorsed:false, baseScale:sz, home:dir.clone(), phase:Math.random()*6.28};
    root.add(spr); incCells.push(spr);
  });
  document.getElementById('statline').textContent =
    `${data.meta.sampled_nodes||NODES.length} of ${data.meta.total_nodes||NODES.length} notes · `+
    `${(data.edges||[]).length} edges · ${incCells.length} incubating`;

  // hubs (top by degree) for the briefing
  HUBS=NODES.map((nd,i)=>({nd,i})).sort((a,b)=>(b.nd.degree||0)-(a.nd.degree||0)).slice(0,10);
  renderBriefing(data);
  updateMembraneStat();        // writes into the briefing's converged section (built above)
  renderDomainLegend();

  // mode chips
  document.querySelectorAll('#modeChips .chip').forEach(c=>
    c.onclick=()=>setColorMode(c.dataset.mode));

  applyLens();   // apply the persisted lens (brightness source, focus, sizes, edge density)
}
// the domains legend — swatches sized by count, click one to isolate that area of the brain
function renderDomainLegend(){
  const el=document.getElementById('legendDom'); if(!el) return;
  const items=[...DOMAINS, DOMAIN_OTHER].filter(d=>(domainCounts[d.key]||0)>0)
    .sort((a,b)=>(domainCounts[b.key]||0)-(domainCounts[a.key]||0));
  el.innerHTML=items.map(d=>`<span class="domsw" data-domain="${d.key}" title="click to isolate · ${domainCounts[d.key]} notes">`
    +`<i class="sw" style="background:${d.color};box-shadow:0 0 7px ${d.color}88"></i>${d.label} <b style="opacity:.45">${domainCounts[d.key]}</b></span>`).join('');
  el.querySelectorAll('[data-domain]').forEach(s=>s.onclick=()=>isolateDomain(s.dataset.domain));
}
let tensionLines=null, tensionHi=null, clusterLines=null, TENSIONS=[], HUBS=[], CONVERGED=[];
let edgeLines=null, edgeCount=0, GMAXDEG=1, GMAXIN=1; const tensionSet=new Set();   // lens state

/* surface a tension: highlight the seam, fly to it, describe it in a box */
function surfaceTension(i){
  const t=TENSIONS[i]; if(!t) return;
  const A=NODES[t.a], B=NODES[t.b];
  const pa=A._pos, pb=B._pos;
  tensionHi.geometry.setAttribute('position',new THREE.BufferAttribute(new Float32Array([pa.x,pa.y,pa.z,pb.x,pb.y,pb.z]),3));
  tensionHi.visible=true;
  // light up the two poles + label them (no neighbour clutter)
  const at=nodePoints.geometry.attributes.aHover; at.array.fill(0); at.array[t.a]=2; at.array[t.b]=2; at.needsUpdate=true;
  hoverIndex=t.a; hoverSet=[t.a,t.b]; pinned=true;
  if(hoverLines) hoverLines.visible=false;
  // fly to the midpoint so both poles are in view, and hold still
  const mid=pa.clone().add(pb).multiplyScalar(0.5);
  focusDir(mid,235); lastZoom=performance.now()/1000;
  $('tenPair').innerHTML=`<span class="tn">${A.title}</span><span class="tx">⟷</span><span class="tn">${B.title}</span>`;
  $('tenBody').textContent=`A productive contradiction (similarity strength ${t.strength}). These two notes are highly related yet pull toward opposing conclusions — the kind of seam worth synthesising rather than collapsing.`
    + (t.demo?'  (demo seam — the live graph currently has 0 detected tensions.)':'');
  $('tensionBox').classList.add('show');
}

/* swap node colours between provenance / recency / domains without rebuilding geometry */
function setColorMode(mode){
  colorMode=mode;
  if(!nodePoints) return;
  const a=nodePoints.geometry.attributes.aColor;
  a.array.set(mode==='recency'?recColArr : mode==='domains'?domColArr : provColArr); a.needsUpdate=true;
  document.querySelectorAll('#modeChips .chip').forEach(c=>
    c.classList.toggle('on', c.dataset.mode===mode));
  document.getElementById('legendProv').style.display = mode==='provenance'?'flex':'none';
  document.getElementById('legendRec').style.display  = mode==='recency'?'flex':'none';
  const ld=document.getElementById('legendDom'); if(ld) ld.style.display = mode==='domains'?'flex':'none';
}

/* ============================ LENS — runtime visual controls (press L) ============================ */
// One persisted state object → applyLens() rebakes brightness(source) + focus-dim + hub-size into the
// node buffers, sets the brightness uniform, and thins edges via drawRange. Modes are just presets.
const LENS_DEFAULT={ mode:'default', source:'heat', focus:0,
  brightFloor:0.42, brightGain:0.95, brightGamma:1.0, hubSize:0.85, edgeFrac:1.0 };
let LENS=(()=>{ try{ return Object.assign({},LENS_DEFAULT,JSON.parse(localStorage.getItem('orbLens')||'{}')); }
            catch(e){ return {...LENS_DEFAULT}; } })();
const saveLens=()=>{ try{ localStorage.setItem('orbLens',JSON.stringify(LENS)); }catch(e){} };
const sstep=(a,b,x)=>{ const t=Math.max(0,Math.min(1,(x-a)/((b-a)||1e-6))); return t*t*(3-2*t); };

const LIFE_VAL={reflective:0.25, crystallizing:0.6, generative:1.0};
const PROV_VAL={originated:1.0, 'ai-inferred':0.95, endorsed:0.7, reference:0.5, encountered:0.30};
// per-node value (0..1) for the current brightness source
function lensSourceValue(nd,i){
  switch(LENS.source){
    case 'in-degree': return Math.sqrt((nd.in_degree||0)/GMAXIN);
    case 'recency':   { const a=nd.age_days==null?999:nd.age_days; return Math.max(0,1-a/30); }
    case 'lifecycle': return LIFE_VAL[nd.lifecycle]||0.3;
    case 'provenance':return PROV_VAL[nd.provenance]||0.3;
    case 'tension':   return tensionSet.has(i)?1:0;
    case 'heat': default: return nd.heat||0;
  }
}
// presets — each re-aims source + colour + focus + brightness into a coherent "mode"
const LENS_MODES={
  default:    {source:'heat',      focus:0,    brightFloor:0.42, brightGain:0.95, hubSize:0.85, colour:null},
  foundations:{source:'in-degree', focus:0.30, brightFloor:0.30, brightGain:1.10, hubSize:1.05, colour:'provenance'},
  recent:     {source:'recency',   focus:0.28, brightFloor:0.32, brightGain:1.00, hubSize:0.70, colour:'recency'},
  provenance: {source:'provenance',focus:0.40, brightFloor:0.32, brightGain:1.00, hubSize:0.80, colour:'provenance'},
  tensions:   {source:'tension',   focus:0.50, brightFloor:0.28, brightGain:1.15, hubSize:0.85, colour:'provenance'},
};
function setLensMode(m){
  const {colour,...knobs}=(LENS_MODES[m]||LENS_MODES.default);
  Object.assign(LENS, knobs, {mode:m});
  if(colour) setColorMode(colour);
  applyLens();
}
// the single place that writes the lens into render state
function applyLens(){
  if(!nodePoints) return;
  const g=nodePoints.geometry;
  const heat=g.attributes.aHeat.array, dim=g.attributes.aDim.array, size=g.attributes.aSize.array;
  const phaseSize={reflective:2.4,crystallizing:3.6,generative:5.6};
  for(let i=0;i<NODES.length;i++){
    const nd=NODES[i];
    const v=Math.max(0,Math.min(1,lensSourceValue(nd,i)));
    heat[i]=v;
    dim[i]= LENS.focus<=0 ? 1 : (0.10 + 0.90*sstep(LENS.focus-0.10, LENS.focus+0.10, v));
    size[i]=(phaseSize[nd.lifecycle]||2.6)*(1+Math.sqrt((nd.degree||0)/GMAXDEG)*LENS.hubSize);
  }
  g.attributes.aHeat.needsUpdate=true; g.attributes.aDim.needsUpdate=true; g.attributes.aSize.needsUpdate=true;
  nodeUniforms.uBright.value.set(LENS.brightFloor, LENS.brightGain, LENS.brightGamma, 0.55);
  if(edgeLines) edgeLines.geometry.setDrawRange(0, Math.round(edgeCount*LENS.edgeFrac)*2);
  if(tensionLines) tensionLines.material.opacity = LENS.mode==='tensions'?0.95:0.6;
  saveLens(); syncLensUI();
}
// panel ↔ state
function syncLensUI(){
  const p=$('lensPanel'); if(!p) return;
  p.querySelectorAll('[data-lmode]').forEach(c=>c.classList.toggle('on', c.dataset.lmode===LENS.mode));
  const src=$('lensSource'); if(src) src.value=LENS.source;
  const set=(id,val,fmt)=>{ const el=$(id); if(el){ el.value=val; const o=$(id+'V'); if(o) o.textContent=fmt(val); } };
  set('lensFloor', LENS.brightFloor, v=>(+v).toFixed(2));
  set('lensGain',  LENS.brightGain,  v=>(+v).toFixed(2));
  set('lensFocus', LENS.focus,       v=>(+v).toFixed(2));
  set('lensHub',   LENS.hubSize,     v=>(+v).toFixed(2));
  set('lensEdges', LENS.edgeFrac,    v=>Math.round(v*100)+'%');
}
function toggleLens(show){
  const el=$('lensPanel'); if(!el) return;
  const on = show==null ? !el.classList.contains('show') : !!show;
  el.classList.toggle('show', on);
  if(on) syncLensUI();
  renderDock();
}
// wire the panel. Uses getElementById (not the `$` helper, which is defined lower
// down) and runs after the module's top-level finishes, so there's no TDZ on `$`.
function wireLens(){
  const p=document.getElementById('lensPanel'); if(!p) return;
  p.querySelectorAll('[data-lmode]').forEach(c=> c.onclick=()=>setLensMode(c.dataset.lmode));
  const src=document.getElementById('lensSource'); if(src) src.onchange=()=>{ LENS.source=src.value; LENS.mode='custom'; applyLens(); };
  const bind=(id,key)=>{ const el=document.getElementById(id); if(el) el.oninput=()=>{ LENS[key]=parseFloat(el.value); LENS.mode='custom'; applyLens(); }; };
  bind('lensFloor','brightFloor'); bind('lensGain','brightGain'); bind('lensFocus','focus');
  bind('lensHub','hubSize'); bind('lensEdges','edgeFrac');
}
if(document.readyState==='loading') addEventListener('DOMContentLoaded', wireLens); else wireLens();
// flat [x,y,z, x,y,z, …] line segments for every edge from `fromIndices` whose
// other end is in `inSet` (each undirected edge emitted once). Shared by the
// domain-isolate and voice topic-cluster highlights.
function edgeSegments(fromIndices, inSet){
  const seg=[], seen=new Set();
  for(const i of fromIndices) for(const c of (adjacency.get(NODES[i].id)||[])){
    const j=idIndex.get(c.id); if(j==null||!inSet.has(j)) continue;
    const k=i<j?i+'_'+j:j+'_'+i; if(seen.has(k)) continue; seen.add(k);
    const p=NODES[i]._pos,o=NODES[j]._pos; seg.push(p.x,p.y,p.z,o.x,o.y,o.z); }
  return seg;
}
// highlight an arbitrary set of nodes + the edges among them (used by domains + topic cluster)
function highlightGroup(indices, frame){
  if(!nodePoints || !indices.length) return 0;
  const set=new Set(indices);
  const a=nodePoints.geometry.attributes.aHover; a.array.fill(0);
  for(const i of indices) a.array[i]=2; a.needsUpdate=true;
  const seg=edgeSegments(indices, set);
  clusterLines.geometry.setAttribute('position',new THREE.BufferAttribute(new Float32Array(seg),3));
  clusterLines.visible=true;
  hoverIndex=indices[0]; hoverSet=indices.slice(0,12); if(hoverLines) hoverLines.visible=false; pinned=true;
  if(frame) frameNodes(indices);   // #70 — fit the whole group (was a fixed focusDir(cen,250))
  return seg.length/6;
}
// isolate one KB domain: colour the orb by domain + light up that domain's nodes & their links
function isolateDomain(key){
  setColorMode('domains');
  const idx=NODES.map((n,i)=>n._domain===key?i:-1).filter(i=>i>=0);
  if(!idx.length){ toast('no '+key+' nodes in view'); return; }
  const links=highlightGroup(idx, true);
  const d=DOMAINS.find(x=>x.key===key)||DOMAIN_OTHER;
  toast('domain · '+d.label+' — '+idx.length+' notes, '+links+' links');
}

/* ============================ inspector + picking + hover ============================ */
const ray=new THREE.Raycaster(); ray.params.Points.threshold=3.4;
const mouse=new THREE.Vector2();
let downX=0,downY=0,dragged=false;
let hoverLines=null, hoverIndex=-1, hoverSet=null, lastInspectedIdx=null;

function pick(cx,cy){
  mouse.x=(cx/innerWidth)*2-1; mouse.y=-(cy/innerHeight)*2+1;
  ray.setFromCamera(mouse,camera);
  // session-captured cells sit outside the shell — check them first
  if(liveNotes.length){
    const lh=ray.intersectObjects(liveNotes.map(n=>n.spr));
    if(lh.length){ const ln=liveNotes.find(n=>n.spr===lh[0].object);
      if(ln){ showDetachedNote(ln.title, ln.content); focusDir(ln.home,215); return; } }
  }
  if(!nodePoints) return;
  const hit=ray.intersectObject(nodePoints);
  if(hit.length){ focusNode(hit[0].index); }   // click on the orb = focus there (same as list)
}
// highlight a node + its neighbours; show their names; draw the connecting edges
function setHover(i){
  if(i===hoverIndex || !nodePoints) return;
  hoverIndex=i;
  if(clusterLines) clusterLines.visible=false;   // a manual hover clears a topic-cluster highlight
  const a=nodePoints.geometry.attributes.aHover; a.array.fill(0);
  if(i>=0){
    a.array[i]=2;
    const ns=(adjacency.get(NODES[i].id)||[]).sort((x,y)=>(y.weight||0)-(x.weight||0)).slice(0,8);
    const seg=[], nb=[];
    for(const c of ns){ const j=idIndex.get(c.id); if(j==null) continue;
      a.array[j]=Math.max(a.array[j],1); nb.push(j);
      const p=NODES[i]._pos, q=NODES[j]._pos; seg.push(p.x,p.y,p.z, q.x,q.y,q.z); }
    hoverLines.geometry.setAttribute('position',new THREE.BufferAttribute(new Float32Array(seg),3));
    hoverLines.visible=true;
    hoverSet=[i,...nb];
    canvas.style.cursor='pointer';
  } else { hoverLines.visible=false; hoverSet=null; canvas.style.cursor=''; }
  a.needsUpdate=true;
}
const $=id=>document.getElementById(id);

/* render a note's markdown + [[wikilinks]] into an element; wikilinks jump if loaded */
const _escHtml=s=>s.replace(/[&<>]/g,m=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[m]));
const _escAttr=s=>s.replace(/"/g,'&quot;').replace(/</g,'&lt;');
function renderNoteInto(el, md){
  if(!md){ el.innerHTML='<span style="opacity:.5">— no body text —</span>'; return; }
  // [[Target]] and [[Target|alias]] -> anchors (done before markdown so the parser leaves them alone)
  let s=md.replace(/\[\[([^\]|#^]+)(?:[#^][^\]|]*)?(?:\|([^\]]+))?\]\]/g,(m,target,alias)=>
    `<a class="wl" data-target="${_escAttr(target.trim())}">${_escHtml((alias||target).trim())}</a>`);
  let html;
  if(window.marked && marked.parse){
    try{ html=marked.parse(s,{breaks:true,gfm:true,mangle:false,headerIds:false}); }
    catch(e){ html=s.replace(/\n/g,'<br>'); }
  } else { html=s.replace(/\n/g,'<br>'); }      // graceful fallback if the CDN didn't load
  // Always sanitize before it reaches the DOM — every path above (marked output
  // AND the raw-markdown fallbacks) is routed through DOMPurify. DOMPurify is
  // vendored locally so it's effectively always present; if it somehow failed to
  // load, degrade to plain text rather than inject unsanitized markup.
  if(window.DOMPurify){ el.innerHTML=DOMPurify.sanitize(html); }
  else { el.textContent=md; }
  // wire wikilink jumps
  el.querySelectorAll('a.wl').forEach(a=>{
    const base=(a.dataset.target||'').toLowerCase().trim();
    const idx=titleIndex.get(base);
    if(idx!=null){ a.classList.add('wl-live'); a.onclick=ev=>{ ev.preventDefault(); focusNode(idx); }; }
    else { a.classList.add('wl-dead'); a.title='not in the loaded graph'; a.onclick=ev=>ev.preventDefault(); }
  });
}

function showInspector(i){
  const nd=NODES[i]; if(!nd) return;
  lastInspectedIdx=i;
  $('inspTitle').textContent=nd.title;
  $('inspLayer').textContent=`${nd.layer} · ${LAYER_NAME[nd.layer]||''}`;
  $('inspPhase').textContent=nd.lifecycle;
  const pv=$('inspProv'); pv.textContent=nd.provenance;
  const c=provColor(nd.provenance); pv.style.color=`#${c.getHexString()}`;
  pv.style.borderColor=`#${c.getHexString()}55`;
  $('inspHeat').textContent=(nd.heat||0).toFixed(2);
  $('inspDeg').textContent=nd.degree||0;
  $('inspInDeg') && ($('inspInDeg').textContent=(nd.in_degree!=null?nd.in_degree:'—'));
  renderNoteInto($('inspContent'), nd.content);
  $('inspector').scrollTop=0;
  const cs=(adjacency.get(nd.id)||[]).sort((a,b)=>(b.weight||0)-(a.weight||0)).slice(0,6);
  const box=$('inspConns');
  box.innerHTML = cs.length? cs.map(c=>{
    const ti=idIndex.get(c.id); const t=NODES[ti]; if(!t) return '';
    return `<div class="conn" data-node="${ti}"><span class="et">${c.type}</span><span>${t.title}</span></div>`;
  }).join('') : '<div class="conn" style="opacity:.5">— none in the sampled view —</div>';
  box.querySelectorAll('[data-node]').forEach(d=>d.onclick=()=>focusNode(+d.getAttribute('data-node')));
  const ia=$('inspActions'); if(ia) ia.style.display = ACTIONS.enabled ? '' : 'none';   // connect needs the backend
  $('inspector').classList.add('show');
}
$('inspClose').onclick=()=>$('inspector').classList.remove('show');

/* ============================ floating labels (fly-inside) ============================ */
const labelBox=document.getElementById('labels'); let labelEls=[];
for(let i=0;i<12;i++){ const d=document.createElement('div'); d.className='nlabel'; labelBox.appendChild(d); labelEls.push(d); }
const _v=new THREE.Vector3();
function updateLabels(){
  // hover takes priority: name the hovered node + the nodes it connects to
  let shown=[], center=hoverIndex;
  if(hoverSet && nodePoints){
    for(const idx of hoverSet){ const n=NODES[idx];
      _v.copy(n._pos).applyMatrix4(root.matrixWorld).project(camera);
      if(_v.z>1) continue;
      shown.push({n,idx,x:(_v.x*0.5+0.5)*innerWidth,y:(-_v.y*0.5+0.5)*innerHeight});
      if(shown.length>=labelEls.length) break;
    }
  } else {
    const inside = camTargetDist < 235;
    if(!inside || !nodePoints){ labelEls.forEach(e=>e.style.opacity=0); return; }
    const cand=NODES.map((n,i)=>({i,n})).sort((a,b)=>(b.n.heat||0)-(a.n.heat||0)).slice(0,40);
    for(const {i,n} of cand){
      _v.copy(n._pos).applyMatrix4(root.matrixWorld).project(camera);
      if(_v.z>1) continue;
      shown.push({n,idx:i,x:(_v.x*0.5+0.5)*innerWidth,y:(-_v.y*0.5+0.5)*innerHeight});
      if(shown.length>=12) break;
    }
  }
  labelEls.forEach((e,k)=>{
    if(k<shown.length){ const s=shown[k]; const isC=s.idx===center;
      e.textContent=s.n.title;
      e.style.left=s.x+'px'; e.style.top=s.y+'px';
      e.style.color=`#${provColor(s.n.provenance).getHexString()}`;
      e.style.opacity=isC?1:.85; e.style.fontWeight=isC?'500':'400';
    } else e.style.opacity=0;
  });
}

/* ============================ visual state machine ============================ */
const STATES={
  idle:    {name:'Idle',      dot:'#00bc7d', desc:'orbiting · gentle breathing · low ambient heat', rot:0.046, breath:0.009, tint:'#63b3ed', tintAmt:0.0},
  hungry:  {name:'Hungry',    dot:'#f0b100', desc:'queue empty · contracting, reaching outward for input', rot:0.028, breath:0.006, tint:'#f0b100', tintAmt:0.18, scale:0.9},
  ingesting:{name:'Ingesting',dot:'#63b3ed', desc:'a new signal streams in and finds its layer', rot:0.050, breath:0.010, tint:'#63b3ed', tintAmt:0.12},
  listening:{name:'Listening',dot:'#aee9ff', desc:'surface ripples with your voice · cool palette', rot:0.034, breath:0.012, tint:'#7fb6ff', tintAmt:0.22},
  thinking:{name:'Thinking',  dot:'#8a7bff', desc:'spreading activation · lateral inhibition · intent aurora', rot:0.062, breath:0.011, tint:'#8a7bff', tintAmt:0.30},
  speaking:{name:'Speaking',  dot:'#ffb86b', desc:'rhythmic pulses synced to output cadence · warm', rot:0.050, breath:0.013, tint:'#ffae5c', tintAmt:0.24},
  converged:{name:'Converged',dot:'#00e0a0', desc:'a thought-cell reached convergence — it hatches', rot:0.046, breath:0.016, tint:'#00e0a0', tintAmt:0.2},
};
let state='idle', cur={rot:0.046,breath:0.009,tintAmt:0,scale:1,tint:new THREE.Color('#63b3ed')};
let stateClock=0;
function setState(s){
  if(!STATES[s]) return; state=s; stateClock=0;
  const S=STATES[s];
  $('stateName').innerHTML=`<span class="dot" style="background:${S.dot};box-shadow:0 0 8px ${S.dot}"></span>${S.name}`;
  $('stateDesc').textContent=S.desc;
  if(s==='thinking') triggerWave(1);
  if(s==='ingesting') igniteRandomNode();
  if(s==='converged') hatchConverged();
}

/* heat ignite: temporarily boost a node's heat attribute (ingesting / new note) */
function igniteRandomNode(){
  if(!nodePoints) return;
  const i=Math.floor(Math.random()*NODES.length);
  const h=nodePoints.geometry.attributes.aHeat; h.array[i]=1.0; h.needsUpdate=true;
  setTimeout(()=>{ h.array[i]=NODES[i].heat||0; h.needsUpdate=true; },2200);
  toast(`ingesting · "${NODES[i].title.slice(0,38)}"`);
}

/* ============================ waves (staleness + thinking) ============================ */
let wave={on:false,type:0,t:0,origin:new THREE.Vector3()};
function triggerWave(type, originIdx){
  let o;
  if(originIdx!=null) o=NODES[originIdx]._pos.clone();
  else { // staleness: prefer a framework node (layer 4)
    const fw=NODES.filter(n=>n.layer===4); const n=(fw.length?fw:NODES)[Math.floor(Math.random()*(fw.length||NODES.length))];
    o=n._pos.clone(); if(type===0) toast(`staleness propagating from "${n.title.slice(0,32)}…"`);
  }
  wave={on:true,type,t:0,origin:o};
}
function updateWave(dt){
  nodeUniforms.uWaveType.value=wave.type;
  if(!wave.on){ nodeUniforms.uWaveOn.value=0; return; }
  wave.t+=dt;
  // ambient sweep (type 2) is slower + wider for a calm screensaver feel
  const radius=wave.t*(wave.type===2 ? 70 : (wave.type? 150:120));
  nodeUniforms.uWaveWidth.value = wave.type===2 ? 56 : 34;
  nodeUniforms.uWaveOn.value=1;
  nodeUniforms.uWave.value.set(wave.origin.x,wave.origin.y,wave.origin.z,radius);
  if(radius>R*2.4){ wave.on=false; nodeUniforms.uWaveOn.value=0;
    if(wave.type===1 && state==='thinking') setTimeout(()=>{ if(state==='thinking') triggerWave(1);},700); }
}

/* ============================ membrane: endorse one ai-inferred ============================ */
let endorsing=[];
function endorseOne(){
  const cand=incCells.filter(c=>!c.userData.endorsed && c.userData.converged);
  const c=cand[0]||incCells.filter(c=>!c.userData.endorsed)[0];
  if(!c){ toast('nothing left to endorse'); return; }
  c.userData.endorsed=true;
  endorsing.push({spr:c,t:0});
  membraneUniforms.uOpen.value=1;             // pore opens
  toast(`endorsed · "${c.userData.topic}" crosses the membrane`);
}
function updateEndorsing(dt){
  if(membraneUniforms.uOpen.value>0) membraneUniforms.uOpen.value=Math.max(0,membraneUniforms.uOpen.value-dt*0.6);
  for(let k=endorsing.length-1;k>=0;k--){
    const e=endorsing[k]; e.t+=dt*0.5; const s=Math.min(1,e.t);
    e.spr.position.lerpVectors(e.spr.userData.home, new THREE.Vector3(0,0,0).addScaledVector(e.spr.userData.home.clone().normalize(), R*0.5), s);
    e.spr.material.color.lerp(new THREE.Color('#63b3ed'), dt*1.2); // amber -> endorsed blue
    if(s>=1){ endorsing.splice(k,1); updateMembraneStat(); }
  }
}
function hatchConverged(){
  const c=incCells.filter(c=>!c.userData.endorsed)[Math.floor(Math.random()*Math.max(1,incCells.length))];
  if(c){ c.userData.converged=true; c.userData.hatch=1.6; toast(`converged · "${c.userData.topic}" hatches`); }
}
function updateMembraneStat(){
  const q=incCells.filter(c=>!c.userData.endorsed).length;
  const e=incCells.filter(c=>c.userData.endorsed).length;
  $('membraneStat').innerHTML=`<b>${q}</b> quarantined · <b>${e}</b> endorsed · only you cross <span style="opacity:.6">(E)</span>`;
}

/* ============================ toast ============================ */
let toastT=null;
function toast(msg){ const el=$('toast'); el.textContent=msg; el.classList.add('show');
  clearTimeout(toastT); toastT=setTimeout(()=>el.classList.remove('show'),2600); }

/* ============================ orientation (quaternion) + fly-to focus ============================ */
const _AXY=new THREE.Vector3(0,1,0), _AXX=new THREE.Vector3(1,0,0);
const _qa=new THREE.Quaternion(), _qb=new THREE.Quaternion();
function rotateWorld(pitch,yaw){
  _qa.setFromAxisAngle(_AXY,yaw); _qb.setFromAxisAngle(_AXX,pitch);
  root.quaternion.premultiply(_qa).premultiply(_qb);
}
let focusing=false; const qFocus=new THREE.Quaternion();
const _front=new THREE.Vector3(0,0,1), _dir=new THREE.Vector3();
function focusDir(vec, dist){
  _dir.copy(vec).normalize(); qFocus.setFromUnitVectors(_dir,_front);
  focusing=true; camTargetDist=dist||205;
}
// #70 — fit-to-selection: center on the selection's centroid direction AND pick a
// zoom distance that frames it, instead of the old fixed 195/250. A single note zooms
// in to its shell; a spread-out cluster zooms out to fit its angular cap in the FOV.
// (Camera sits at (0,0,camDist) looking at origin; focusDir rotates the target to front.)
const _fcen=new THREE.Vector3(), _fp=new THREE.Vector3();
function frameNodes(indices){
  if(!indices || !indices.length) return;
  _fcen.set(0,0,0); let n=0;
  for(const i of indices){ const p=NODES[i]&&NODES[i]._pos; if(p){ _fcen.add(p); n++; } }
  if(!n || _fcen.length()<1e-3) return;
  _fcen.multiplyScalar(1/n);
  const dir=_fcen.clone().normalize();
  let theta=0, rMax=0;                                  // angular spread + farthest shell
  for(const i of indices){ const p=NODES[i]&&NODES[i]._pos; if(!p) continue;
    rMax=Math.max(rMax, p.length());
    const a=_fp.copy(p).normalize().angleTo(dir); if(a>theta) theta=a; }
  const fov=((camera.fov||45)*Math.PI/180);
  const lateral=rMax*Math.sin(theta);                  // half-extent of the cap across view
  const depth=rMax*Math.cos(theta);                    // near face of the cap
  const fit=depth + (lateral/Math.tan(fov/2)) + 60;    // fit the cap vertically + margin
  focusDir(dir, zclamp(Math.max(rMax+45, fit)));       // never inside the node shell
  lastZoom=performance.now()/1000;
}
function focusNode(i){
  const nd=NODES[i]; if(!nd) return;
  showInspector(i); setHover(i);       // light up the node + its neighbours + draw the connecting edges
  // #70/#71 — frame the node TOGETHER WITH its connected neighbours (setHover's
  // hoverSet), so a just-made link's other endpoint is on-screen — not a lone node
  // zoomed in while its connection sits off-frame.
  frameNodes(hoverSet && hoverSet.length ? hoverSet : [i]);
  pinned=true;                         // keep the selection lit; a stray mouse move must not wipe it
  const h=nodePoints.geometry.attributes.aHeat; h.array[i]=1.0; h.needsUpdate=true;
  setTimeout(()=>{ h.array[i]=nd.heat||0; h.needsUpdate=true; },1700);
}

/* ============================ briefing rail (surface what matters) ============================ */
function renderBriefing(data){
  const m=data.metrics||{}, ph=m.phases||{};
  const total=(ph.reflective||0)+(ph.crystallizing||0)+(ph.generative||0)||1;
  const pct=x=>Math.round(100*(x||0)/total);
  const act=(data.activity||[]);
  const conv=(data.converged||[]);
  const rec=(data.recent||[]);
  const cards=(m.cards||[]).slice(0,4);

  // Panel 1 — live activity
  $('brief1').innerHTML=`
    <div class="bgrp">
      <div class="bh">◇ live pulse</div>
      <div class="pulse">
        <div class="pmain"><span class="pdot"></span>${act[0]?act[0].kind:'idle'}</div>
        <div class="psub">${act[0]?('last activity · '+act[0].ago):'no recent runs'}</div>
      </div>
      <div class="feed">${act.slice(0,6).map(a=>
        `<div class="frow"><span class="fk fk-${a.state}">${a.kind}</span>`+
        `<span class="fl">${a.label}</span><span class="fa">${a.ago}</span></div>`).join('')}</div>
    </div>`;

  // Panel 2 — health
  $('brief2').innerHTML=`
    <div class="bgrp">
      <div class="bh">▦ health</div>
      <div class="chips">${cards.map(c=>
        `<div class="mchip"><div class="mv">${fmt(c.value)}</div><div class="ml">${c.label}</div></div>`).join('')}</div>
      <div class="lcbar" title="lifecycle phases">
        <span style="flex:${ph.reflective||1}" class="lc lc-r"></span>
        <span style="flex:${ph.crystallizing||1}" class="lc lc-c"></span>
        <span style="flex:${Math.max(ph.generative||0,0.5)}" class="lc lc-g"></span>
      </div>
      <div class="lclegend">
        <span><i class="d lc-r"></i>refl ${pct(ph.reflective)}%</span>
        <span><i class="d lc-c"></i>cryst ${pct(ph.crystallizing)}% <b>(${fmt(m.bottleneck||0)})</b></span>
        <span><i class="d lc-g"></i>gen ${ph.generative||0}</span>
      </div>
      <div class="callout">⚠ <b>${fmt(m.bottleneck||0)}</b> to synthesize · ✦ <b>${m.learned_7d!=null?m.learned_7d:'—'}</b> new/7d</div>
    </div>`;

  // Panel 3 — converged conclusions + tensions + hubs (navigate / review). Scrolls.
  $('brief3').innerHTML=`
    <div class="bgrp" data-sec="converged">
      <div class="bh bh-toggle" data-toggle="converged"><span class="caret">▾</span> ⬡ converged thinking <span class="cnt">${conv.length}</span></div>
      <div class="secbody">
        <div class="list" id="convList">${conv.map((c,i)=>
          `<div class="li" data-conv="${i}"><span class="lidot amber"></span><span class="lit">${c.topic}</span></div>`).join('')}</div>
        <div class="bnote membrane" id="membraneStat"></div>
      </div>
    </div>
    ${TENSIONS.length?`<div class="bgrp" data-sec="tensions">
      <div class="bh bh-toggle" data-toggle="tensions"><span class="caret">▾</span> ⚡ tensions <span class="cnt">${TENSIONS.length}</span></div>
      <div class="secbody">
        <div class="list">${TENSIONS.map((t,i)=>
          `<div class="li" data-tension="${i}"><span class="lidot red"></span><span class="lit">${NODES[t.a].title} <span style="opacity:.5">⟷</span> ${NODES[t.b].title}</span></div>`).join('')}</div>
      </div>
    </div>`:''}
    <div class="bgrp" data-sec="hubs">
      <div class="bh bh-toggle" data-toggle="hubs"><span class="caret">▾</span> ★ hubs <span class="cnt">${HUBS.length}</span></div>
      <div class="secbody">
        <div class="list">${HUBS.map(h=>
          `<div class="li" data-node="${h.i}"><span class="lidot blue"></span><span class="lit">${h.nd.title}</span><span class="lideg">${h.nd.degree}</span></div>`).join('')}</div>
      </div>
    </div>`;

  // #60: the brief cards' innerHTML was just replaced, which wiped the boot-time
  // close ✕ — re-inject it so every widget keeps its visible close button.
  ['brief1','brief2','brief3'].forEach(id=>{
    const el=$(id); if(!el || el.querySelector('.tile-x')) return;
    const x=document.createElement('span'); x.className='tile-x'; x.textContent='✕';
    x.title='hide this tile (re-open it from the dock)';
    x.onclick=ev=>{ ev.stopPropagation(); setTile(id, false); };
    el.appendChild(x);
  });

  // collapsible sections (converged / tensions / hubs) — collapsed state persisted in localStorage
  const collapsed=loadCollapsed();
  $('brief3').querySelectorAll('.bh-toggle').forEach(h=>{
    const key=h.dataset.toggle, grp=h.closest('.bgrp');
    if(collapsed.has(key)) grp.classList.add('collapsed');
    h.onclick=()=>{ const isC=grp.classList.toggle('collapsed');
      if(isC) collapsed.add(key); else collapsed.delete(key); saveCollapsed(collapsed); };
  });

  // wire clicks across all three panels
  ['brief1','brief2','brief3'].forEach(id=>{
    const el=$(id);
    el.querySelectorAll('[data-node]').forEach(li=>{
      const i=li.getAttribute('data-node'); if(i==='') return;
      li.onclick=()=>focusNode(+i);
    });
    el.querySelectorAll('[data-conv]').forEach(li=>
      li.onclick=()=>openConclusion(conv[+li.getAttribute('data-conv')]));
    el.querySelectorAll('[data-tension]').forEach(li=>
      li.onclick=()=>surfaceTension(+li.getAttribute('data-tension')));
  });
}
const fmt=v=> (v==null?'—': (v>=1000? (v/1000).toFixed(v>=10000?0:1)+'k' : ''+v));
/* collapsed briefing sections persist as a list of section keys in localStorage */
function loadCollapsed(){ try{ return new Set(JSON.parse(localStorage.getItem('orbSections')||'[]')); }catch(e){ return new Set(); } }
function saveCollapsed(set){ try{ localStorage.setItem('orbSections', JSON.stringify([...set])); }catch(e){} }

/* reading panel: the real converged conclusion */
function openConclusion(c){
  if(!c) return;
  $('readTopic').textContent=c.topic;
  $('readQ').textContent=c.question||'';
  $('readBody').textContent=c.conclusion||'';
  $('readRuns').textContent=c.runs?`${c.runs} runs`:'';
  $('reading').classList.add('show');
  // fly to the matching incubation cell + bloom it
  const cell=incCells.find(x=>x.userData.topic===c.topic);
  if(cell){ focusDir(cell.userData.home,230); cell.userData.hatch=Math.max(cell.userData.hatch||0,1.2); }
}
$('readClose')&&($('readClose').onclick=()=>$('reading').classList.remove('show'));
function closeTension(){ $('tensionBox').classList.remove('show'); if(tensionHi) tensionHi.visible=false; }
$('tenClose')&&($('tenClose').onclick=closeTension);

/* ============================ auto-demo timeline ============================ */
let auto=false, autoIdx=0, autoT=0, autoDur=9;
const TIMELINE=[['idle',10],['ingesting',6],['thinking',11],['idle',7],['speaking',6],['hungry',8],['converged',5],['idle',8]];
function updateAuto(dt){
  if(!auto) return; autoT+=dt;
  if(autoT>autoDur){ autoT=0; autoIdx=(autoIdx+1)%TIMELINE.length;
    autoDur=TIMELINE[autoIdx][1]*(0.8+Math.random()*0.5);   // dwell time varies each cycle
    setState(TIMELINE[autoIdx][0]); if(Math.random()<0.35) triggerWave(0); }
}

/* ============================ ambient idle choreography (screensaver feel) ============================ */
// While the orb is just sitting idle, every so often it plays a small, slow flourish so it feels alive
// without ever speeding up. Never fires during interaction (drag / hover / pin / focus / recent zoom).
let ambientT=0, ambientGap=14, ambientGlow=0;
function ambientFlourish(){
  const r=Math.random();
  if(r<0.55){                              // a soft wave of light sweeps across the orb
    triggerWave(2);
  } else if(r<0.82){                       // a scatter of nodes twinkles, then fades
    twinkleShower();
  } else {                                 // the whole orb softly blooms brighter and settles
    ambientGlow=0.7;
  }
}
function twinkleShower(){
  if(!nodePoints) return;
  const f=nodePoints.geometry.attributes.aFresh, n=NODES.length, picks=[];
  for(let k=0;k<10;k++){ const i=Math.floor(Math.random()*n); picks.push(i); f.array[i]=Math.max(f.array[i],1); }
  f.needsUpdate=true;
  setTimeout(()=>{ for(const i of picks){ const a=NODES[i].age_days==null?999:NODES[i].age_days;
    f.array[i]=Math.max(0,Math.min(1,1-a/7)); } f.needsUpdate=true; }, 4200);
}
function updateAmbient(dt, t){
  const calm = state==='idle' && !dragging && !focusing && !wave.on
            && hoverIndex<0 && !pinned && !auto && (t-lastZoom)>=FREEZE_IDLE_S;
  if(!calm){ ambientT=0; ambientGlow=Math.max(0,ambientGlow-dt*0.25); return; }   // stay quiet while busy
  ambientT+=dt;
  if(ambientT>=ambientGap){ ambientT=0; ambientGap=20+Math.random()*28; ambientFlourish(); }   // every ~20-48s
  ambientGlow=Math.max(0,ambientGlow-dt*0.22);
}

/* ============================ find-and-jump search ============================ */
const searchInput=$('searchInput'), searchResultsEl=$('searchResults');
let sResults=[], sSel=0;
function runSearch(raw){
  const q=raw.trim().toLowerCase();
  if(!q){ sResults=[]; searchResultsEl.classList.remove('show'); searchResultsEl.innerHTML=''; return; }
  const scored=[];
  for(let i=0;i<NODES.length;i++){
    const idx=NODES[i].title.toLowerCase().indexOf(q);
    if(idx<0) continue;
    scored.push({i, s:(idx===0?0:100000)+idx*100-(NODES[i].degree||0)});  // prefix > earlier > more-connected
  }
  scored.sort((a,b)=>a.s-b.s);
  sResults=scored.slice(0,9).map(x=>x.i); sSel=0; renderResults(q);
}
function renderResults(q){
  searchResultsEl.classList.add('show');
  if(!sResults.length){ searchResultsEl.innerHTML='<div class="snone">no matches in the loaded graph</div>'; return; }
  searchResultsEl.innerHTML=sResults.map((i,k)=>{
    const n=NODES[i], c=provColor(n.provenance).getHexString(), t=n.title, p=t.toLowerCase().indexOf(q);
    const title=p<0?_escHtml(t):_escHtml(t.slice(0,p))+'<b>'+_escHtml(t.slice(p,p+q.length))+'</b>'+_escHtml(t.slice(p+q.length));
    return `<div class="sresult ${k===sSel?'sel':''}" data-i="${i}" data-k="${k}">`+
      `<span class="sdot" style="background:#${c};box-shadow:0 0 6px #${c}"></span>`+
      `<span class="stitle">${title}</span><span class="slayer">${LAYER_NAME[n.layer]||''}</span></div>`;
  }).join('');
  searchResultsEl.querySelectorAll('.sresult').forEach(el=>{
    el.onmousedown=ev=>{ ev.preventDefault(); jumpTo(+el.dataset.i); };       // mousedown keeps input focused
    el.onmouseenter=()=>{ sSel=+el.dataset.k; markSel(); };
  });
}
function markSel(){ [...searchResultsEl.children].forEach((el,k)=>el.classList&&el.classList.toggle('sel',k===sSel)); }
function jumpTo(i){ if(linkFrom!=null){ doLink(i); searchInput.blur(); grabFocus(); return; }   // link mode: pick = connect
  focusNode(i); searchResultsEl.classList.remove('show'); searchInput.blur(); grabFocus(); }
searchInput.addEventListener('input',()=>runSearch(searchInput.value));
searchInput.addEventListener('keydown',e=>{
  if(e.key==='ArrowDown'){ e.preventDefault(); sSel=Math.min(sSel+1,sResults.length-1); markSel(); searchResultsEl.children[sSel]?.scrollIntoView({block:'nearest'}); }
  else if(e.key==='ArrowUp'){ e.preventDefault(); sSel=Math.max(sSel-1,0); markSel(); searchResultsEl.children[sSel]?.scrollIntoView({block:'nearest'}); }
  else if(e.key==='Enter'){ e.preventDefault(); if(sResults[sSel]!=null) jumpTo(sResults[sSel]); }
  else if(e.key==='Escape'){ endLinkMode(); searchInput.value=''; runSearch(''); searchInput.blur(); grabFocus(); }
});

/* ============================ voice tile + orb-control bridge ============================ */
let VOICE_PROXY='';   // brokered base (set from the embed init in loadData) — '' standalone
let detachedNote=null;                        // a note opened from the vault but not in the graph
// robust title match against the loaded nodes (handles .md, partials, word-order, sub/superstrings)
// #103: hyphens normalize to spaces too — exporters may title inbox nodes by file
// stem ("20260706-140610-my-note"), which must match a search for "my note".
function normTitle(s){ return (s||'').toLowerCase().replace(/\.md$/,'').replace(/[-_]+/g,' ').replace(/\s+/g,' ').trim(); }
function searchNodeIds(raw, limit){
  const q=normTitle(raw); if(!q) return [];
  const words=q.split(' ').filter(w=>w.length>2);
  const scored=[];
  for(let i=0;i<NODES.length;i++){
    const t=normTitle(NODES[i].title); let s=null;
    if(t===q) s=-1000;
    else if(t.startsWith(q)) s=-600+t.length;
    else if(t.includes(q)) s=t.indexOf(q);
    else if(t.length>4 && q.includes(t)) s=300;                 // model gave a longer phrase
    else if(words.length){ const hit=words.filter(w=>t.includes(w)).length;
      if(hit===words.length) s=500;
      else if(hit>=Math.ceil(words.length*0.6)) s=900-hit*20; } // partial word overlap
    if(s!=null) scored.push({i, s: s - (NODES[i].degree||0)*0.01});
  }
  scored.sort((a,b)=>a.s-b.s); return scored.slice(0,limit||8).map(x=>x.i);
}
// light up a whole topic cluster: every related node + the edges between them
function highlightTopic(query){
  if(!nodePoints) return {count:0};
  const q=normTitle(query); if(!q) return {count:0};
  const words=q.split(' ').filter(w=>w.length>2);
  const match=t=>{ t=normTitle(t); return t.includes(q) || (words.length && words.every(w=>t.includes(w))); };
  // scope-aware first: if the topic names a mounted scope (a book, the company, a
  // research area), light up that whole scope's rendered cluster — "highlight what is
  // in scope". A book's notes aren't titled after the book (its hub is "_book"), so a
  // title match alone can never find them; match by scope membership instead.
  let core=[];
  const stok = (typeof resolveScopeToken==='function') ? resolveScopeToken(query) : null;
  if(stok && stok!=='core'){
    const pfx=stok+'/';
    for(let i=0;i<NODES.length;i++){
      const id=NODES[i].id||'';
      const inScope = NODES[i].scope===stok || id===stok || id.startsWith(pfx) || id.split('/')[0]===stok;
      if(inScope && !/^CHANGELOG\b/i.test(NODES[i].title||'')) core.push(i);   // skip operational changelog notes that physically live in the scope folder
    }
  }
  // otherwise: title matches, then content mentions (the topic isn't a visible scope)
  if(!core.length) for(let i=0;i<NODES.length;i++) if(match(NODES[i].title)) core.push(i);
  const _noise=i=>/^CHANGELOG\b/i.test(NODES[i].title||'');   // operational exhaust, never a topic cluster
  if(!core.length) for(let i=0;i<NODES.length && core.length<14;i++)
    if(!_noise(i) && (NODES[i].content||'').toLowerCase().includes(q)) core.push(i);
  // last resort: a conceptual phrase the voice invents ("model failure under radical
  // uncertainty") that is no note's exact title — light up nodes that contain MOST of
  // its significant words (title or content), ranked by how many, so the right cluster
  // still appears instead of nothing.
  if(!core.length){
    const sig=q.split(' ').filter(w=>w.length>3);
    if(sig.length>=2){
      const need=Math.max(2, Math.ceil(sig.length*0.6)), sc=[];
      for(let i=0;i<NODES.length;i++){
        if(_noise(i)) continue;
        const hay=(NODES[i].title+' '+(NODES[i].content||'')).toLowerCase();
        const hits=sig.reduce((a,w)=>a+(hay.includes(w)?1:0),0);
        if(hits>=need) sc.push({i,hits});
      }
      sc.sort((a,b)=>b.hits-a.hits); core=sc.slice(0,14).map(x=>x.i);
    }
  }
  if(!core.length) return {count:0, note:'no related notes in the visible graph — they may be off-canvas'};
  core=core.sort((a,b)=>(NODES[b].degree||0)-(NODES[a].degree||0)).slice(0,16);
  const coreSet=new Set(core);
  // neighbours of the core within the loaded graph (the connective tissue)
  const nbr=new Set();
  for(const i of core) for(const c of (adjacency.get(NODES[i].id)||[])){
    const j=idIndex.get(c.id); if(j!=null && !coreSet.has(j)) nbr.add(j); }
  const nbrArr=[...nbr].slice(0,50);
  // highlight: core brightest, neighbours dim
  const a=nodePoints.geometry.attributes.aHover; a.array.fill(0);
  for(const i of core) a.array[i]=2;
  for(const j of nbrArr) a.array[j]=Math.max(a.array[j],1);
  a.needsUpdate=true;
  // edges connecting the highlighted set (core↔core and core↔neighbour)
  const hi=new Set([...core,...nbrArr]);
  const seg=edgeSegments(core, hi);
  clusterLines.geometry.setAttribute('position',new THREE.BufferAttribute(new Float32Array(seg),3));
  clusterLines.visible=true;
  // labels = the core titles; frame the camera on the cluster centroid and hold still
  hoverIndex=core[0]; hoverSet=core.slice(0,12); if(hoverLines) hoverLines.visible=false; pinned=true;
  const cen=new THREE.Vector3(); for(const i of core) cen.add(NODES[i]._pos); cen.multiplyScalar(1/core.length);
  if(cen.length()>1) focusDir(cen, 250);
  lastZoom=performance.now()/1000;
  toast('voice · lit up '+core.length+' "'+query.slice(0,28)+'" notes');
  return {count:core.length, neighbours:nbrArr.length, connections:seg.length/6,
          nodes:core.map(i=>NODES[i].title)};
}
// open arbitrary note content (fetched from the vault) in the inspector, without a graph node
function showDetachedNote(title, content){
  detachedNote={title, content}; lastInspectedIdx=null;
  $('inspTitle').textContent=title;
  $('inspLayer').textContent='—'; $('inspPhase').textContent='—';
  const pv=$('inspProv'); pv.textContent='in vault · not on canvas'; pv.style.color='var(--color-mist)'; pv.style.borderColor='rgba(255,255,255,.12)';
  $('inspHeat').textContent='—'; $('inspDeg').textContent='—';
  renderNoteInto($('inspContent'), content); $('inspector').scrollTop=0;
  $('inspConns').innerHTML='<div class="conn" style="opacity:.5">— not a node in the current graph view —</div>';
  const ia=$('inspActions'); if(ia) ia.style.display='none';   // can't connect a node that isn't on the canvas
  $('inspector').classList.add('show');
}
// tools the voice can call to drive the visualisation
const ORB_TOOLS={
  search_graph({query}){ return {matches: searchNodeIds(query,8).map(i=>NODES[i].title)}; },
  highlight_related_notes({topic}){ return highlightTopic(topic); },
  async navigate_to_note({title}){
    let ids=searchNodeIds(title,1);
    // #71 — a just-created note may not be folded in yet (writes refresh on a debounce).
    // If we miss AND an integration is pending or running, flush/join it and retry before
    // falling back to the vault — so "create X, now show me X" lands on the real node.
    // #103 — but NEVER dead-air past the voice tile's 8s tool timeout: a real-vault
    // reindex can take minutes. Wait a bounded ~6s; if it's still integrating, answer
    // honestly and let the pending-focus auto-select land it when the rebuild completes.
    if(!ids.length && (_refreshPending || refreshBusy)){
      if(!_pendingFocus) _setPendingFocus({title});   // ensure auto-select on completion
      const run=(refreshBusy && _refreshRun) ? _refreshRun : refreshGraph('navigate-miss');
      const out=await Promise.race([
        Promise.resolve(run).catch(()=>null),
        new Promise(res=>setTimeout(()=>res('__timeout'),6000)),
      ]);
      if(out==='__timeout')
        return {opened:false, integrating:true,
                note:'that note is still being folded into the graph — it will be highlighted automatically once integration finishes'};
      ids=searchNodeIds(title,1);
    }
    if(ids.length){ const i=ids[0]; focusNode(i);
      return {opened:true, in_view:true, title:NODES[i].title, layer:LAYER_NAME[NODES[i].layer],
              preview:(NODES[i].content||'').replace(/\s+/g,' ').trim().slice(0,300)}; }
    // not among the loaded nodes — search the whole vault via the read-only KB
    // broker (#60 Phase 3: POST /tool → the agent's ~/.trinity/brain-orb/search
    // hook, scope-aware, read-only). The hook returns a note or a results list.
    try{
      const r=await fetch(VOICE_PROXY+'/tool', {method:'POST',
        headers:{'Content-Type':'application/json',...ORB_HEADERS},
        body:JSON.stringify({query:title, limit:1}), signal:AbortSignal.timeout(8000)});
      if(!r.ok) return {opened:false, error:'no note matching "'+title+'" found in the vault'};
      const d=await r.json();
      const hit = (d.results && d.results[0]) || d;   // accept {results:[…]} or a bare note
      if(!hit || !hit.content) return {opened:false, error:'no note matching "'+title+'" found in the vault'};
      showDetachedNote(hit.title||title, hit.content); toast('voice · opened "'+(hit.title||title).slice(0,40)+'"');
      return {opened:true, in_view:false, title:hit.title||title,
              preview:hit.content.replace(/\s+/g,' ').trim().slice(0,300)};
    }catch(e){ return {opened:false, error:'could not load the note ('+(e.message||e)+')'}; }
  },
  // authoritative enumeration of the agent's converged conclusions — the voice must
  // call this instead of guessing from semantic recall. Also lights the converged set.
  list_converged_topics(){
    const topics=CONVERGED.map(c=>({topic:c.topic, question:(c.question||'').replace(/\s+/g,' ').trim().slice(0,200)}));
    const idx=[]; for(let i=0;i<NODES.length;i++) if(NODES[i].converged) idx.push(i);
    if(idx.length){ highlightGroup(idx, true); pinned=true; toast('voice · '+topics.length+' converged conclusions'); }
    return {count:topics.length, on_canvas:idx.length, topics};
  },
  surface_tensions(){ if(!TENSIONS.length) return {count:0, note:'no tensions detected in the loaded graph'};
    surfaceTension(0);
    return {count:TENSIONS.length, tensions:TENSIONS.slice(0,6).map(t=>({a:NODES[t.a].title,b:NODES[t.b].title,strength:t.strength}))}; },
  list_hubs(){ const idx=HUBS.map(h=>h.i); if(idx.length) highlightGroup(idx, true);   // light the hubs up, not just list them
    return {hubs:HUBS.map(h=>({title:h.nd.title, connections:h.nd.degree})), highlighted:idx.length}; },
  set_colour_mode({mode}){ if(!['provenance','recency','domains'].includes(mode)) return {error:"mode must be 'provenance', 'recency', or 'domains'"};
    setColorMode(mode); toast('voice · colour: '+mode); return {mode}; },
  read_current_note(){
    if(lastInspectedIdx!=null && NODES[lastInspectedIdx])
      return {title:NODES[lastInspectedIdx].title, content:(NODES[lastInspectedIdx].content||'').slice(0,4000)};
    if(detachedNote) return {title:detachedNote.title, content:(detachedNote.content||'').slice(0,4000)};
    return {error:'no note is open on screen'};
  },
  // voice-initiated skills: run in the background (no page reload, so the voice session survives)
  find_connections({topic}){
    if(!ACTIONS.enabled) return {error:'the action backend is not available'};
    runSkill('find-connections', topic?('"'+topic+'"'):'', false);
    return {started:true, skill:'find-connections', topic:topic||'(recent notes)',
            note:'running in the background; results land in the knowledge base'};
  },
  synthesize_insights({topic}){
    if(!ACTIONS.enabled) return {error:'the action backend is not available'};
    runSkill('synthesize-insights', topic?('"'+topic+'"'):'', false);
    return {started:true, skill:'synthesize-insights', topic:topic||'(recent)',
            note:'running in the background; results land in the knowledge base'};
  },
  async capture_note({title, body}){
    if(!ACTIONS.enabled) return {error:'the action backend is not available'};
    const text=(body||title||'').trim();
    if(!text) return {error:'nothing to capture'};
    const r=await postAction('capture',{title:title||'', body:text});
    if(r&&r.ok){
      // #103 — creation outcome is confirmed on screen, never ambiguous.
      toast('voice · captured "'+String(r.title||text.split('\n')[0]).slice(0,40)+'" — integrating…');
      _setPendingFocus(r, text.split('\n')[0].slice(0,60));   // #72 — auto-select once integrated
      scheduleRefresh();   // #67 — fold it into the real graph (debounced across a burst)
      return {ok:true, title:r.title, recorded_to:r.path,
              note:'recorded and being integrated into the graph — it will appear and be selected shortly'}; }
    toast('voice · capture FAILED: '+((r&&r.error)||'unknown error'));   // #103 — a failed write is loud
    return {error:(r&&r.error)||'capture failed'};
  },
  // #61 Phase 4a — voice-triggered link (owner sessions only; the write tools are in the
  // locked manifest only when the minting user owns the agent). Connects two notes by title.
  async link_notes({source, target}){
    if(!ACTIONS.enabled) return {error:'the action backend is not available'};
    if(!source||!target) return {error:'need both a source and a target note'};
    const r=await postAction('link',{from:source, to:target});
    if(r&&r.ok){ if(!r.already) scheduleRefresh();   // #67 — persist the edge into the real graph
      return {ok:true, linked:[source, target], already:!!r.already}; }
    return {error:(r&&r.error)||'link failed'};
  },
  // ---- scope tools: bring a book/company/research INTO view + into what the voice can search ----
  list_scopes(){ return {active:SCOPE_ACTIVE,
    available:SCOPE_AVAIL.map(s=>({token:s.token, label:s.label, kind:s.kind,
      mounted:_isMounted(s.token, s.parent)}))}; },
  async mount_scope({scope}){
    const t=resolveScopeToken(scope); if(!t) return {error:'no scope matching "'+scope+'" — try list_scopes'};
    const already=_isMounted(t);
    if(!already){
      const fams=familyTokens();
      let next=SCOPE_ACTIVE.slice(); if(fams.has(t)) next=next.filter(x=>!x.startsWith(t+'/')); next.push(t);
      const r=await setScope(next);   // awaits the in-place rebuild — graph holds the new scope before we highlight
      if(!(r&&r.ok)) return {error:(r&&r.error)||'mount failed'};
    }
    // mounting brings the scope into the graph; light its cluster up too so it's immediately
    // VISIBLE (not just searchable) — "pull in X" both shows X and highlights it in one move,
    // whether it was freshly mounted or already on the canvas.
    let lit=null;
    if(t!=='core'){ const sa=SCOPE_AVAIL.find(s=>s.token===t); try{ lit=highlightTopic(sa?sa.label:t); }catch(_){ } }
    return {ok:true, mounted:t, already, active:SCOPE_ACTIVE, lit:(lit&&lit.count)||0};
  },
  async unmount_scope({scope}){
    const t=resolveScopeToken(scope); if(!t) return {error:'no scope matching "'+scope+'" — try list_scopes'};
    const r=await setScope(SCOPE_ACTIVE.filter(x=>x!==t && x.split('/')[0]!==t));
    return (r&&r.ok)?{ok:true, unmounted:t, active:SCOPE_ACTIVE}:{error:(r&&r.error)||'unmount failed'};
  },
};
// tools that visibly highlight the orb — their result should persist (not be wiped by a mouse move)
const VOICE_DISPLAY_TOOLS=new Set(['navigate_to_note','highlight_related_notes','surface_tensions','list_hubs','list_converged_topics']);
let pinned=false;   // a deliberate highlight (node/hub/search click · domain · tension · voice) is pinned — hover won't wipe it until the user takes over
addEventListener('message', e=>{
  if(e.origin!==window.location.origin) return;   // same-origin only — ORB_TOOLS now includes owner-gated writes (capture_note/link_notes), so don't dispatch cross-origin
  const m=e.data; if(!m || m.type!=='orb-tool' || !e.source) return;
  let out;
  try{ const fn=ORB_TOOLS[m.name]; out = fn ? fn(m.args||{}) : {error:'unknown tool '+m.name}; }
  catch(err){ out={error:String(err&&err.message||err)}; }
  if(VOICE_DISPLAY_TOOLS.has(m.name)) pinned=true;   // hold it until the user takes over
  Promise.resolve(out).then(o=>{ try{ e.source.postMessage({type:'orb-tool-result', id:m.id, output:o}, window.location.origin); }catch(_){ } });
});
// #66 — the voice tile relays its finished conversation (it never holds the JWT);
// we POST it to the owner-gated broker as capture_transcript, with the session id as
// the Idempotency-Key so a double session-end saves exactly ONE transcript. process:true
// asks the platform to run the configured post-session prompt as a real execution.
// #102 — every outcome is surfaced: saved / post-processing started / skipped (+why) /
// failed. A session end must never be silently indistinguishable from a failure.
addEventListener('message', e=>{
  if(e.origin!==window.location.origin) return;   // same-origin voice iframe only
  const m=e.data; if(!m || m.type!=='orb-capture-transcript') return;
  const hasDialogue=(m.events||[]).some(ev=>ev&&(ev.event==='user_turn'||ev.event==='model_turn'));
  if(!hasDialogue){ toast('voice session ended — no dialogue to save'); return; }
  if(!ACTIONS.enabled){ toast('voice transcript NOT saved — agent offline or KB writes unavailable'); return; }
  postAction('capture_transcript', {session_id:m.session_id, events:m.events||[], process:true}, m.session_id)
    .then(r=>{
      if(r&&r.ok&&r.saved){
        const pp=r.postprocess;
        if(pp&&pp.dispatched) toast('voice transcript saved · post-processing started (see Executions)');
        else if(pp&&pp.reason) toast('voice transcript saved · post-processing skipped: '+pp.reason);
        else toast('voice transcript saved');
      }
      else if(r&&r.ok) toast('voice transcript not saved: '+(r.reason||'nothing to save'));
      else toast('voice transcript NOT saved: '+((r&&r.error)||'unknown error'));
    });
});
// #60 Phase 3 — the voice tile (a nested iframe) never holds the platform JWT.
// It asks US to mint a short-lived Gemini ephemeral token via the JWT-gated broker,
// and we relay just the token back. The JWT stays in this page; the voice iframe
// only ever sees the single-use, model-locked Google token (safe for the browser).
addEventListener('message', async e=>{
  if(e.origin!==window.location.origin) return;   // same-origin voice iframe only
  const m=e.data; if(!m || m.type!=='orb-voice-token-request' || !e.source) return;
  // Replies origin-pinned — the ok-reply carries the ephemeral token (same-origin iframe).
  const VOICE_ORIGIN = window.location.origin;
  if(!VOICE_PROXY){ try{ e.source.postMessage({type:'orb-voice-token', ok:false, error:'voice not available'}, VOICE_ORIGIN); }catch(_){ } return; }
  try{
    const r=await fetch(VOICE_PROXY+'/voice-token', {method:'POST', headers:{...ORB_HEADERS}, signal:AbortSignal.timeout(12000)});
    if(!r.ok){ e.source.postMessage({type:'orb-voice-token', ok:false, error:'mint failed ('+r.status+')'}, VOICE_ORIGIN); return; }
    const d=await r.json();
    e.source.postMessage({type:'orb-voice-token', ok:true, token:d.ephemeral_token, model:d.model, voiceName:d.voice_name, expiresAt:d.expires_at}, VOICE_ORIGIN);
  }catch(err){ try{ e.source.postMessage({type:'orb-voice-token', ok:false, error:String(err&&err.message||err)}, VOICE_ORIGIN); }catch(_){ } }
});
function toggleVoice(){
  const t=$('voiceTile'); const open=t.classList.toggle('show');
  if(open){ const f=$('voiceFrame');
    // #60: opening the tile AUTO-STARTS the conversation (no manual Start click).
    // First open loads the page (which auto-starts on load); a re-open of an
    // already-loaded (ended) tile re-starts via a message.
    if(!f.getAttribute('src')) f.setAttribute('src','./voice/orb.html?v='+Date.now());
    else { try{ f.contentWindow.postMessage({type:'orb-voice-start'}, window.location.origin); }catch(_){} }
    toast('voice on — starting…'); }
  else {
    pinned=false;
    // turning voice off must DISCONNECT (mic + Gemini Live socket), not just hide the tile
    const f=$('voiceFrame');
    try{ f && f.contentWindow && f.contentWindow.postMessage({type:'orb-voice-stop'}, window.location.origin); }catch(_){}
    toast('voice off - session disconnected');
  }
  renderDock();   // keep the dock's voice chip in sync
}
$('voiceClose')&&($('voiceClose').onclick=()=>toggleVoice());

/* ============================ scope: mount/unmount vault scopes live (press S) ============================ */
// One mutable mount-set lives in the proxy (:8770). Toggling here drives BOTH the orb
// render (server re-export) AND what the voice can search. The rebuild is IN-PLACE (no
// page reload) so the voice session/iframe survives a scope change.
let SCOPE_AVAIL=[], SCOPE_ACTIVE=[], scopeBusy=false;
const familyTokens=()=>new Set(SCOPE_AVAIL.filter(s=>s.family).map(s=>s.token));
function toggleScopePanel(show){
  const el=$('scopePanel'); if(!el) return;
  const on = show==null ? !el.classList.contains('show') : !!show;
  el.classList.toggle('show', on);
  if(on){ loadScopes(); clampPanels(); }   // clamp once visible — a real rect now exists, so an off-screen restored position is pulled back on-screen
  renderDock();
}
async function loadScopes(){
  try{
    const r=await fetch(VOICE_PROXY+'/scopes',{headers:ORB_HEADERS,signal:AbortSignal.timeout(6000)});
    const d=await r.json();
    SCOPE_AVAIL=d.available||[]; SCOPE_ACTIVE=d.active||[];
    renderScopePanel();
  }catch(e){
    const l=$('scopeList'); if(l) l.innerHTML='<div class="scopehint">scope control needs the brain-orb backend (run /brain-orb)</div>';
  }
}
function _isMounted(tok, parent){ return SCOPE_ACTIVE.includes(tok) || (!!parent && SCOPE_ACTIVE.includes(parent)); }
// best-effort node count for a scope token on the CURRENT canvas (from the loaded nodes)
function _scopeCount(tok){
  if(tok==='core') return NODES.reduce((a,n)=>a+(n.core?1:0),0);
  if(tok.includes('/')) return NODES.reduce((a,n)=>a+((n.id||'').startsWith(tok+'/')?1:0),0);
  return NODES.reduce((a,n)=>a+(n.scope===tok?1:0),0);
}
function renderScopePanel(){
  const l=$('scopeList'); if(!l) return;
  if(!SCOPE_AVAIL.length){ l.innerHTML='<div class="scopehint">no scopes reported by the backend</div>'; return; }
  const core=SCOPE_AVAIL.filter(s=>s.core);
  const cog =SCOPE_AVAIL.filter(s=>!s.core && !s.family && !s.parent && s.kind!=='reference');
  const books=SCOPE_AVAIL.filter(s=>s.family || s.parent);
  const ref =SCOPE_AVAIL.filter(s=>s.kind==='reference');
  const row=s=>{
    const on=_isMounted(s.token, s.parent);
    const cls='scoperow'+(s.parent?' child':'')+(s.kind==='reference'?' ref':'');
    const tag = s.family ? '<span class="cnt">shelf</span>'
              : (()=>{ const c=_scopeCount(s.token); return `<span class="cnt">${c||'·'}</span>`; })();
    return `<label class="${cls}"><input type="checkbox" data-token="${s.token}" data-parent="${s.parent||''}"${on?' checked':''}>${s.label}${tag}</label>`;
  };
  const sec=(title,items)=> items.length ? `<div class="scopegrp">${title}</div>`+items.map(row).join('') : '';
  l.innerHTML = sec('core', core)+sec('cognitive', cog)+sec('books', books)+sec('reference', ref);
  l.querySelectorAll('input[data-token]').forEach(cb=> cb.onchange=()=>onScopeToggle(cb));
}
function onScopeToggle(cb){
  if(scopeBusy){ cb.checked=!cb.checked; return; }            // ignore while a remount is in flight
  const tok=cb.dataset.token, parent=cb.dataset.parent, fams=familyTokens();
  let next=SCOPE_ACTIVE.slice();
  if(cb.checked){
    if(fams.has(tok)) next=next.filter(t=>!t.startsWith(tok+'/'));   // mounting a shelf supersedes its books
    if(!next.includes(tok)) next.push(tok);
  } else {
    if(parent && SCOPE_ACTIVE.includes(parent)){
      // unchecking one book while its shelf is mounted → expand the shelf to the OTHER books
      next=next.filter(t=>t!==parent);
      next.push(...SCOPE_AVAIL.filter(s=>s.parent===parent && s.token!==tok).map(s=>s.token));
    } else {
      next=next.filter(t=>t!==tok);
      if(fams.has(tok)) next=next.filter(t=>!t.startsWith(tok+'/'));  // unchecking a shelf drops its books
    }
  }
  setScope(next);
}
// resolve a spoken/typed scope name → a mount token (for the voice tools)
function resolveScopeToken(name){
  const q=normTitle(name); if(!q) return null;
  let hit=SCOPE_AVAIL.find(s=>s.token.toLowerCase()===q || normTitle(s.label)===q);
  if(hit) return hit.token;
  if(q==='core'||q==='fingerprint') return 'core';
  if(q.includes('compan')){ const c=SCOPE_AVAIL.find(s=>s.kind==='reference'); return c?c.token:'Company'; }
  if(q.includes('document')||q.includes('research')||q==='insights') return 'Document Insights';
  hit=SCOPE_AVAIL.find(s=>s.token.toLowerCase().includes(q)||normTitle(s.label).includes(q));
  if(hit) return hit.token;
  const words=q.split(' ').filter(w=>w.length>2);
  hit=SCOPE_AVAIL.find(s=>s.parent && words.length && words.every(w=>s.token.toLowerCase().includes(w)));
  if(hit) return hit.token;
  if(q.includes('book')) return 'Books';                      // whole shelf (after specific-book match fails)
  return null;
}
async function setScope(tokens){
  // #58 Phase 2: gated on the Trinity embed (platform JWT), not the old per-start
  // voice-proxy token. The backend owner-gates the mutating /scope route.
  if(!SCOPE_ENABLED){ toast('scope control unavailable'); return {error:'no backend'}; }
  if(scopeBusy) return {error:'busy'};
  scopeBusy=true; const sp=$('scopePanel'); sp&&sp.classList.add('busy');
  const ld=$('loading'); if(ld){ ld.textContent='remounting scope…'; ld.style.display=''; }
  try{
    const r=await fetch(VOICE_PROXY+'/scope',{method:'POST',
      headers:{'Content-Type':'application/json',...ORB_HEADERS},
      body:JSON.stringify({tokens})});
    const res=await r.json();
    if(res&&res.ok){
      SCOPE_ACTIVE=res.active||tokens;
      const data=await (await fetch(VOICE_PROXY+'/data',{headers:ORB_HEADERS})).json();
      applyData(data);
      renderScopePanel();
      toast('scope · '+SCOPE_ACTIVE.length+' mounted · '+(res.nodes!=null?res.nodes:NODES.length)+' notes');
    } else { toast('scope change failed'+(res&&res.error?': '+res.error:'')); }
    return res;
  }catch(e){ toast('scope change failed ('+(e.message||e)+')'); return {error:String(e&&e.message||e)}; }
  finally{ scopeBusy=false; sp&&sp.classList.remove('busy'); if(ld) ld.style.display='none'; }
}
// #66/#67 — close the write → reindex/re-export → refetch loop. After a capture/link
// (or a manual refresh), ask the agent to fold new inbox notes/links into the graph,
// then refetch /data and rebuild IN PLACE (same machinery as setScope). #68: a visible
// "integrating…" state + a result toast so the user can confirm the write landed.
let refreshBusy=false, _refreshTimer=null, _refreshPending=false;   // #67/#71 shared refresh state
let _refreshRun=null, _refreshQueued=false;   // #103 — in-flight promise (join, don't drop) + one queued follow-up
// #72/#103 — a just-created note to auto-select once the refresh folds it in. Multiple
// keys: exporters may title the node by H1 OR by file stem, so we try both.
let _pendingFocus=null;   // {keys:[...], tries:0}
function _setPendingFocus(r, fallback){
  const keys=[];
  if(r&&r.title) keys.push(String(r.title));
  const p=(r&&r.path)?String(r.path):'';
  const m=p.match(/([^\/]+)\.md$/); if(m) keys.push(m[1]);   // file stem (#103 — real exporters title inbox nodes by stem)
  if(fallback) keys.push(fallback);
  if(keys.length) _pendingFocus={keys, tries:0};
}
function _findPending(){ if(!_pendingFocus) return -1;
  for(const k of _pendingFocus.keys){ const ids=searchNodeIds(k,1); if(ids.length) return ids[0]; }
  return -1; }
function _pendingInData(data){   // cheap presence probe — no teardown/rebuild just to look
  if(!_pendingFocus) return false;
  const nodes=(data&&data.nodes)||[];
  return _pendingFocus.keys.some(k=>{ const nk=normTitle(k); if(!nk) return false;
    return nodes.some(n=>{ const t=normTitle(n&&n.title); return t===nk||t.includes(nk)||(t.length>4&&nk.includes(t)); }); });
}
// #103 — the exporter may fold a fresh write in asynchronously (or report net-zero
// counts), so after a refresh that didn't surface the note, poll /data briefly instead
// of declaring "up to date" and silently dropping it. Rebuilds only when it appears.
function _pollPendingFocus(){
  if(!_pendingFocus) return;
  if(_pendingFocus.tries>=3){
    toast('note saved — not on the graph yet (integrate & refresh again in a moment)');
    _pendingFocus=null; return;
  }
  _pendingFocus.tries++;
  setTimeout(async()=>{
    if(!_pendingFocus) return;
    if(refreshBusy||scopeBusy){ _pollPendingFocus(); return; }   // a rebuild is happening anyway
    try{
      const data=await (await fetch(VOICE_PROXY+'/data',{headers:ORB_HEADERS})).json();
      if(_pendingInData(data)){
        applyData(data);
        const pi=_findPending();
        if(pi>=0){ focusNode(pi); toast('note integrated · selected'); }
        _pendingFocus=null;
      } else _pollPendingFocus();
    }catch(_){ _pollPendingFocus(); }
  },2500);
}
async function refreshGraph(reason){
  if(!VOICE_PROXY || !SCOPE_ENABLED) return {error:'no backend'};
  if(scopeBusy) return {error:'busy'};
  if(refreshBusy && _refreshRun){
    // #103 — a refresh is already integrating: join it instead of dropping this call,
    // and queue ONE follow-up so a write that landed mid-export still gets folded in.
    _refreshQueued=true;
    return _refreshRun;
  }
  const run=_refreshOnce(reason);
  _refreshRun=run;
  let res;
  try{ res=await run; }
  finally{
    _refreshRun=null;
    if(_refreshQueued){ _refreshQueued=false; scheduleRefresh(); }
  }
  return res;
}
async function _refreshOnce(reason){
  _refreshPending=false; clearTimeout(_refreshTimer);   // consume any pending debounced refresh
  // #72 — snapshot the selection BY ID before the rebuild (teardownGraph wipes
  // lastInspectedIdx/hover and node indices change), so we can re-select it after.
  const selId=(lastInspectedIdx!=null && NODES[lastInspectedIdx]) ? NODES[lastInspectedIdx].id : null;
  const selGroup=(!selId && hoverSet && hoverSet.length>1)
    ? hoverSet.map(j=>NODES[j] && NODES[j].id).filter(Boolean) : null;
  refreshBusy=true; const ld=$('loading'); if(ld){ ld.textContent='integrating…'; ld.style.display=''; }
  try{ setState('ingesting'); }catch(_){ }
  try{
    const r=await fetch(VOICE_PROXY+'/refresh',{method:'POST',headers:{...ORB_HEADERS}});
    let res=null; try{ res=await r.json(); }catch(_){ }
    if(!r.ok){ res={error:(res&&(res.detail||res.error))||('refresh failed ('+r.status+')')}; }
    if(res&&res.ok){
      const an=res.added_nodes||0, ae=res.added_edges||0;
      if(an||ae){   // #72 — rebuild ONLY when the graph actually changed (a no-op refresh keeps the selection)
        const data=await (await fetch(VOICE_PROXY+'/data',{headers:ORB_HEADERS})).json();
        applyData(data);
        // #72 — after a write, auto-SELECT the just-created note (no need to ask again);
        // otherwise restore the pre-refresh selection so a mid-task refresh doesn't deselect.
        let focused=false;
        const pi=_findPending();
        if(pi>=0){ focusNode(pi); _pendingFocus=null; focused=true; }
        if(!focused){
          if(selId!=null){ const ni=idIndex.get(selId); if(ni!=null) focusNode(ni); }
          else if(selGroup && selGroup.length){ const idx=selGroup.map(id=>idIndex.get(id)).filter(v=>v!=null);
            if(idx.length) highlightGroup(idx, true); }
        }
        toast('graph updated · +'+an+' notes, +'+ae+' links');
      } else { toast('graph up to date'); }   // nothing new → no rebuild, selection intact
      // #103 — a just-written note still missing (async exporter / net-zero counts /
      // stem-titled node): don't drop it — try once on the current graph, then poll.
      if(_pendingFocus){
        const pi2=_findPending();
        if(pi2>=0){ focusNode(pi2); _pendingFocus=null; }
        else _pollPendingFocus();
      }
    } else { toast('refresh failed'+(res&&res.error?': '+res.error:'')); }   // keep _pendingFocus — the next refresh can still land it
    return res;
  }catch(e){ toast('refresh failed ('+(e.message||e)+')'); return {error:String(e&&e.message||e)}; }
  finally{ refreshBusy=false; if(ld) ld.style.display='none'; try{ setState('idle'); }catch(_){ } }
}
// tear down the per-build THREE objects + reset the collections buildGraph appends to,
// so applyData can rebuild the scene in place (no reload → the voice socket survives)
function teardownGraph(){
  [nodePoints,hoverLines,edgeLines,tensionLines,tensionHi,clusterLines].forEach(o=>{
    if(!o) return; root.remove(o); if(o.geometry) o.geometry.dispose();
  });
  incCells.forEach(s=>{ root.remove(s); if(s.material) s.material.dispose(); });
  nodePoints=hoverLines=edgeLines=tensionLines=tensionHi=clusterLines=null; incCells.length=0;
  NODES.length=0; idIndex.clear(); titleIndex.clear(); adjacency.clear();
  pinned=false; hoverIndex=-1; hoverSet=null; lastInspectedIdx=null; detachedNote=null;
  const insp=$('inspector'); if(insp) insp.classList.remove('show');
}
function applyData(data){
  teardownGraph();
  buildGraph(data);
  setColorMode(colorMode);   // re-apply the active colour mode to the fresh buffers
  setState('idle');
}

/* ============================ KB actions (capture · connect) — #61 Phase 4a ============================ */
// Owner-gated writes via the Trinity broker (POST /api/agents/{name}/brain-orb/action),
// authed with the platform JWT (ORB_HEADERS) — NOT the old localhost proxy / X-Orb-Token.
// The panel un-hides (body.brain-orb-write) only after the broker confirms owner + flag +
// agent write hook. run_skill (exec) + transcript capture are Phase 4b (trinity-enterprise#66).
const ACTIONS={enabled:false, skills:[]};
// Fresh idempotency key per write attempt (Invariant #18): a client retry dedups at the
// broker instead of double-writing. randomUUID is available same-origin (HTTPS/localhost).
function _idemKey(){ try{ return crypto.randomUUID(); }catch(_){ return 'k'+Date.now()+Math.random().toString(36).slice(2); } }
async function initActions(){
  if(!WRITE_AVAILABLE || !VOICE_PROXY){ ACTIONS.enabled=false; return; }   // platform flag off / standalone
  try{
    const r=await fetch(VOICE_PROXY+'/actions',{headers:ORB_HEADERS,signal:AbortSignal.timeout(6000)});
    if(r.ok){ ACTIONS.enabled=true; }   // 200 ⇒ owner + write flag + agent ships the action hook
  }catch(e){ ACTIONS.enabled=false; }   // 403 (non-owner) / 404 (flag off / no hook) / offline → stay hidden
  ACTIONS.skills=[];                     // run_skill deferred to Phase 4b (#66) — no skill buttons in 4a
  const sb=$('skillBtns'); if(sb) sb.innerHTML='';
  if(ACTIONS.enabled){ try{ document.body.classList.add('brain-orb-write'); }catch(_){} }  // reveal capture/connect
}
async function postAction(type,args,idemKey){
  if(!ACTIONS.enabled){ toast('KB writes are not available for this agent'); return {error:'no backend'}; }
  try{
    const r=await fetch(VOICE_PROXY+'/action',{method:'POST',
      headers:{'Content-Type':'application/json','Idempotency-Key':idemKey||_idemKey(),...ORB_HEADERS},
      body:JSON.stringify({action:type, ...(args||{})})});
    let d=null; try{ d=await r.json(); }catch(_){ }
    // #103 — surface broker errors: FastAPI errors carry `detail` (503 "Agent is not
    // running", 404 flag off), which callers checking `r.error` used to render as '?'.
    if(!r.ok) return {error:(d&&(d.detail||d.error))||('request failed ('+r.status+')')};
    return d||{error:'empty response'};
  }catch(e){ return {error:String(e&&e.message||e)}; }
}
function setActStatus(msg,run){ const el=$('actStatus'); if(!el) return; el.textContent=msg||''; el.classList.toggle('run',!!run); }
function toggleActions(force){
  const p=$('actions'); if(!p) return;
  const open = force!=null?force : !p.classList.contains('show');
  p.classList.toggle('show',open);
  if(open){ if(!ACTIONS.enabled) setActStatus('backend offline — capture/skills disabled'); setTimeout(()=>$('capInput')&&$('capInput').focus(),50); }
}
$('actClose')&&($('actClose').onclick=()=>toggleActions(false));
// #68 — visible manual "integrate & refresh" control (reindex + re-export + rebuild).
$('refreshGraphBtn')&&($('refreshGraphBtn').onclick=()=>refreshGraph('manual'));
// #67 — voice-created writes come in bursts; coalesce them into ONE rebuild a few
// seconds after the last one so the orb isn't torn down mid-conversation per capture.
// _refreshPending lets navigate_to_note flush the pending integration on a miss (#71).
function scheduleRefresh(){ _refreshPending=true; clearTimeout(_refreshTimer);
  _refreshTimer=setTimeout(()=>{ _refreshPending=false; refreshGraph('voice'); },4000); }

/* --- capture a thought -> a note in 00-Inbox (the note lands now; it joins the graph at the next reindex) --- */
// Re-entrancy guard: each postAction mints a fresh Idempotency-Key, so a double-click
// would otherwise write two notes. Bounce the second click while one is in flight.
let _capInFlight=false;
async function doCapture(){
  if(_capInFlight) return;
  const ta=$('capInput'); const body=(ta.value||'').trim();
  if(!body){ toast('type a thought first'); return; }
  _capInFlight=true;
  setActStatus('capturing…',true);
  let r; try{ r=await postAction('capture',{body}); } finally { _capInFlight=false; }
  if(r&&r.ok){ ta.value=''; setActStatus(''); toggleActions(false);
    toast('captured → '+(r.title||'inbox').slice(0,40));
    // #67 — fold it into the actual graph so it appears as a real, connectable node.
    // #72 — auto-select the new note once integrated (no separate "select it" step).
    _setPendingFocus(r, body.split('\n')[0].slice(0,60));
    refreshGraph('capture'); }
  else setActStatus('capture failed: '+((r&&r.error)||'?'));
}
$('capSave')&&($('capSave').onclick=doCapture);
$('capInput')&&$('capInput').addEventListener('keydown',e=>{
  if((e.metaKey||e.ctrlKey)&&e.key==='Enter'){ e.preventDefault(); doCapture(); }
  e.stopPropagation();   // typing here must not fire orb hotkeys
});

/* --- run an allow-listed skill headlessly; poll the job, then reload to surface changes ---
   Phase 4b (trinity-enterprise#66): DEFERRED. Kept for the follow-up but currently INERT —
   ACTIONS.skills is [] (no skill buttons) and the voice manifest declares no run_skill tool,
   so this is never invoked; the broker /action route rejects action=run_skill (400) and there
   is no /job route in 4a. Wired up with the detached-execution rework in #66. */
let jobTimer=null;
async function runSkill(skill,args,reload=true){
  const r=await postAction('run_skill',{skill,args:args||''});
  if(!r||!r.ok){ setActStatus('could not start: '+((r&&r.error)||'?')); return; }
  toast('running /'+skill+' …'); setActStatus('/'+skill+' · running… (may take a while)',true);
  setState(skill==='incubation-loop'?'thinking':'ingesting');
  clearInterval(jobTimer);
  jobTimer=setInterval(async()=>{
    let d; try{ d=await (await fetch(VOICE_PROXY+'/job?id='+r.job)).json(); }catch(e){ return; }
    if(!d || d.status==='running') return;
    clearInterval(jobTimer); setState('idle');
    if(d.status==='done'){
      if(reload){ setActStatus('/'+skill+' done · refreshing…'); toast('/'+skill+' finished — refreshing'); setTimeout(()=>location.reload(),1400); }
      else { setActStatus('/'+skill+' done'); toast('/'+skill+' finished (results in the KB)'); } }
    else { setActStatus('/'+skill+' '+d.status); toast('/'+skill+' '+d.status); }
  },2500);
}

/* --- connect two notes: pick a target via the search box in "link mode", append a [[wikilink]] --- */
let linkFrom=null, liveEdges=null, liveSeg=[];
function ensureLiveEdges(){ if(liveEdges) return;
  liveEdges=new THREE.LineSegments(new THREE.BufferGeometry(),
    new THREE.LineBasicMaterial({color:0x9fd6ff,transparent:true,opacity:.6,depthWrite:false,blending:THREE.AdditiveBlending}));
  root.add(liveEdges); }
function addLiveEdge(aIdx,bIdx){ ensureLiveEdges();
  const p=NODES[aIdx]._pos,o=NODES[bIdx]._pos; liveSeg.push(p.x,p.y,p.z,o.x,o.y,o.z);
  liveEdges.geometry.setAttribute('position',new THREE.BufferAttribute(new Float32Array(liveSeg),3)); }
function registerLink(aId,bId){
  if(!adjacency.has(aId)) adjacency.set(aId,[]);
  if(!adjacency.has(bId)) adjacency.set(bId,[]);
  adjacency.get(aId).push({id:bId,type:'references',weight:0.6});
  adjacency.get(bId).push({id:aId,type:'references',weight:0.6});
}

/* session captures: a note made this session shows as a persistent bright cell + always-on label,
   sitting just outside the shell so it stays well visible until the next reindex folds it in. */
let liveNotes=[];
function addLiveNote(title, content){
  const dir=fib(liveNotes.length*5+2, 60).multiplyScalar(R*1.12);
  const spr=new THREE.Sprite(new THREE.SpriteMaterial({
    map:GLOW, color:new THREE.Color('#dffaff'), transparent:true, opacity:.95,
    depthWrite:false, blending:THREE.AdditiveBlending}));
  spr.position.copy(dir); spr.scale.set(17,17,1); spr.userData={phase:Math.random()*6.28};
  root.add(spr);
  const label=document.createElement('div'); label.className='nlabel livelabel'; label.textContent=title;
  document.getElementById('labels').appendChild(label);
  liveNotes.push({spr, title, content, label, home:dir.clone()});
  focusDir(dir,230);   // fly to the freshly-captured note so it's front-and-centre
}
function updateLiveNotes(){
  for(const ln of liveNotes){
    const s=17*(1+0.13*Math.sin(mt*1.5+ln.spr.userData.phase)); ln.spr.scale.set(s,s,1);
    _v.copy(ln.spr.position).applyMatrix4(root.matrixWorld).project(camera);
    const front=_v.z<1;
    ln.label.style.opacity=front?'1':'0';
    if(front){ ln.label.style.left=(_v.x*0.5+0.5)*innerWidth+'px'; ln.label.style.top=(-_v.y*0.5+0.5)*innerHeight+'px'; }
  }
}
function startLink(){
  if(lastInspectedIdx==null || !NODES[lastInspectedIdx]){ toast('open a note first'); return; }
  linkFrom={idx:lastInspectedIdx, title:NODES[lastInspectedIdx].title};
  $('search').classList.add('linkmode');
  searchInput.placeholder='link "'+linkFrom.title.slice(0,20)+'" → find target…';
  searchInput.value=''; runSearch(''); searchInput.focus();
  toast('pick a note to connect to "'+linkFrom.title.slice(0,26)+'"');
}
function endLinkMode(){ if(linkFrom==null) return; linkFrom=null;
  $('search').classList.remove('linkmode'); searchInput.placeholder='find a note…   press  /  to search'; }
async function doLink(toIdx){
  const from=linkFrom, to=NODES[toIdx]; endLinkMode();
  searchResultsEl.classList.remove('show'); searchInput.value='';
  if(!from || !to) return;
  if(toIdx===from.idx){ toast('cannot link a note to itself'); return; }
  const r=await postAction('link',{from:from.title, to:to.title});
  if(r&&r.ok){ addLiveEdge(from.idx,toIdx); registerLink(NODES[from.idx].id,to.id);
    toast(r.already?'already linked':'linked → '+to.title.slice(0,26)); focusNode(from.idx);
    if(!r.already) refreshGraph('link'); }   // #67 — persist the edge into the real graph
  else toast('link failed: '+((r&&r.error)||'?'));
}
$('iaConnect')&&($('iaConnect').onclick=startLink);

/* ============================ input ============================ */
addEventListener('keydown',e=>{
  const ae=document.activeElement;                       // typing in a field — don't trigger orb shortcuts
  if(ae && /^(INPUT|TEXTAREA)$/.test(ae.tagName)) return;
  const k=e.key.toLowerCase();
  if(k==='/'){ e.preventDefault(); searchInput.focus(); searchInput.select(); return; }
  const map={'1':'idle','2':'hungry','3':'ingesting','4':'listening','5':'thinking','6':'speaking','7':'converged'};
  if(map[k]) setState(map[k]);
  else if(k==='w') triggerWave(0);
  else if(k==='e') endorseOne();
  else if(k==='p'){ auto=!auto; toast(auto?'auto-demo on':'auto-demo off'); }
  else if(k==='r'){ const seq=['provenance','recency','domains']; const next=seq[(seq.indexOf(colorMode)+1)%seq.length];
    setColorMode(next);
    toast(next==='recency'?'colour: recency (fresh = bright)':next==='domains'?'colour: domains (click a domain to isolate)':'colour: provenance'); }
  else if(k==='b'){ const anyShown=['brief1','brief2','brief3'].some(id=>!hiddenTiles.has(id));
    ['brief1','brief2','brief3'].forEach(id=>setTile(id,!anyShown)); }   // B = toggle all briefs (via the tile system)
  else if(k==='v'){ toggleVoice(); }
  else if(k==='l'){ toggleLens(); }
  else if(k==='s'){ toggleScopePanel(); }   // C is reserved for a future control
  else if(k==='a'){ toggleActions(); }
  else if(k==='escape'){ pinned=false; endLinkMode(); toggleActions(false);
    $('reading').classList.remove('show'); $('inspector').classList.remove('show'); closeTension(); setHover(-1); }
});
let downTarget=null;
addEventListener('pointerdown',e=>{ downTarget=e.target; }, true);   // capture: who was clicked
canvas.addEventListener('pointerdown',e=>{downX=e.clientX;downY=e.clientY;dragged=false;dragging=true;px=e.clientX;py=e.clientY;pinned=false;});
addEventListener('pointerup',e=>{ dragging=false;
  if(downTarget===canvas && !dragged) pick(e.clientX,e.clientY); });   // only pick when the orb itself was clicked
let dragging=false,px=0,py=0;
addEventListener('pointermove',e=>{
  if(dragging){
    const dx=e.clientX-px, dy=e.clientY-py; px=e.clientX;py=e.clientY;
    if(Math.abs(e.clientX-downX)+Math.abs(e.clientY-downY)>4) dragged=true;
    focusing=false;                     // grabbing cancels a fly-to
    rotateWorld(dy*0.005, dx*0.005);
    return;
  }
  // a pinned selection (click / hub / search / domain / tension / voice) survives mouse movement until the user takes over
  if(pinned) return;
  // hover: highlight the node under the cursor + its connections
  if(!nodePoints || e.target!==canvas){ if(hoverIndex>=0) setHover(-1); return; }
  mouse.x=(e.clientX/innerWidth)*2-1; mouse.y=-(e.clientY/innerHeight)*2+1;
  ray.setFromCamera(mouse,camera);
  const hit=ray.intersectObject(nodePoints);
  setHover(hit.length?hit[0].index:-1);
});

/* ---- zoom: trackpad scroll over the void + pinch (ctrl+wheel) + Safari gestures ---- */
const ZMIN=112, ZMAX=720;   // ZMIN keeps the deepest zoom just inside the node shell
const zclamp=v=>Math.max(ZMIN,Math.min(ZMAX,v));
const markZoom=()=>{ lastZoom=performance.now()/1000; };   // freeze ambient motion; resume after FREEZE_IDLE_S
// can any scrollable element between `node` and `stop` actually scroll in direction dy?
function panelCanScroll(node, stop, dy){
  let el=node;
  while(el){
    if(el.scrollHeight>el.clientHeight+1){
      const oy=getComputedStyle(el).overflowY;
      if(oy==='auto'||oy==='scroll'){
        const atTop=el.scrollTop<=0, atBottom=el.scrollTop+el.clientHeight>=el.scrollHeight-1;
        if((dy<0 && !atTop)||(dy>0 && !atBottom)) return true;   // it has somewhere to go
      }
    }
    if(el===stop) break;
    el=el.parentElement;
  }
  return false;
}
addEventListener('wheel',e=>{
  if(e.ctrlKey){                       // trackpad pinch -> always zoom, anywhere, never page-zoom
    e.preventDefault();
    camTargetDist=zclamp(camTargetDist + e.deltaY*1.6); markZoom(); return;
  }
  // over a scrollable panel: let it scroll, but only while it can — once it hits the edge, zoom out
  const panel = e.target.closest && e.target.closest('#brief3,#reading,#inspector,#searchResults,#voiceTile,#tensionBox');
  if(panel && panelCanScroll(e.target, panel, e.deltaY)) return;
  e.preventDefault();
  camTargetDist=zclamp(camTargetDist + e.deltaY*0.28); markZoom();
},{passive:false});
let _gd=0;
addEventListener('gesturestart',e=>{ e.preventDefault(); _gd=camTargetDist; markZoom(); });
addEventListener('gesturechange',e=>{ e.preventDefault(); if(e.scale) camTargetDist=zclamp(_gd/e.scale); markZoom(); });
addEventListener('gestureend',e=>{ e.preventDefault(); markZoom(); });

addEventListener('resize',()=>{
  camera.aspect=innerWidth/innerHeight; camera.updateProjectionMatrix();
  renderer.setSize(innerWidth,innerHeight); nodeUniforms.uPixelRatio.value=renderer.getPixelRatio();
  clampPanels();
});

/* ============================ movable HUD panels ============================ */
const INTERACTIVE='button,a,input,select,textarea,.li,.chip,.close,.vclose,.dom,.bh-toggle,.tile-x,.dchip,#inspContent,#readBody,#tenBody,[data-node],[data-conv],.conn';
function makeDraggable(el){
  if(!el) return;
  el.style.cursor='move'; el.style.userSelect='none';
  el.addEventListener('pointerdown',e=>{
    if(e.button!==0 || (e.target.closest && e.target.closest(INTERACTIVE))) return;
    const r=el.getBoundingClientRect();
    el.style.left=r.left+'px'; el.style.top=r.top+'px';
    el.style.right='auto'; el.style.bottom='auto'; el.style.transform='none';
    const ox=e.clientX-r.left, oy=e.clientY-r.top; let moved=false;
    el.setPointerCapture(e.pointerId); el.classList.add('grabbing');
    const move=ev=>{
      const nx=Math.max(4,Math.min(innerWidth-44, ev.clientX-ox));
      const ny=Math.max(4,Math.min(innerHeight-30, ev.clientY-oy));
      el.style.left=nx+'px'; el.style.top=ny+'px'; moved=true;
    };
    const up=()=>{ el.releasePointerCapture(e.pointerId); el.classList.remove('grabbing');
      el.removeEventListener('pointermove',move); el.removeEventListener('pointerup',up);
      if(moved) savePanels(); };
    el.addEventListener('pointermove',move); el.addEventListener('pointerup',up);
    e.stopPropagation();
  });
}
// the on-demand popups (reading = converged conclusion, tensionBox) are floating widgets too
const DRAGGABLE=['title','brief1','brief2','brief3','stateBox','legend','controls','lensPanel','scopePanel','inspector','voiceTile','reading','tensionBox','dock'];
// every draggable panel persists its position (a panel only saves once it has actually been moved,
// so un-dragged panels keep their CSS default placement)
const PERSIST=DRAGGABLE;
function savePanels(){ try{ const p={}; PERSIST.forEach(id=>{const el=document.getElementById(id);
  if(el&&el.style.left) p[id]={l:el.style.left,t:el.style.top};}); localStorage.setItem('orbPanels',JSON.stringify(p)); }catch(e){} }
function restorePanels(){ try{ const p=JSON.parse(localStorage.getItem('orbPanels')||'{}');
  for(const id in p){ const el=document.getElementById(id); if(!el)continue;
    el.style.left=p[id].l; el.style.top=p[id].t; el.style.right='auto'; el.style.bottom='auto'; el.style.transform='none'; }
  clampPanels(); }catch(e){} }
function clampPanels(){ DRAGGABLE.forEach(id=>{ const el=document.getElementById(id);
  if(!el||!el.style.left||el.classList.contains('tile-hidden'))return; const r=el.getBoundingClientRect();
  if(!r.width && !r.height) return;   // a hidden (display:none) panel reports a 0×0 rect at 0,0 — clamping here would pin it to the corner and WIPE the position restorePanels just set (breaks persistence for scope/lens/reading/tension/voice). It gets clamped when it's actually shown.
  el.style.left=Math.max(4,Math.min(innerWidth-44,r.left))+'px';
  el.style.top =Math.max(4,Math.min(innerHeight-30,r.top))+'px'; }); }
DRAGGABLE.forEach(id=>makeDraggable(document.getElementById(id)));
restorePanels();

/* ---- tiles: close any HUD panel (✕ on hover) and re-open it from the dock; state persisted ---- */
const TILES=[['title','cornelius'],['brief1','pulse'],['brief2','health'],['brief3','lists'],
             ['stateBox','state'],['legend','legend'],['controls','keys']];
function loadHidden(){ try{ return new Set(JSON.parse(localStorage.getItem('orbHidden')||'[]')); }catch(e){ return new Set(); } }
function saveHidden(s){ try{ localStorage.setItem('orbHidden', JSON.stringify([...s])); }catch(e){} }
const hiddenTiles=loadHidden();
function applyTile(id){ const el=document.getElementById(id); if(el) el.classList.toggle('tile-hidden', hiddenTiles.has(id)); }
function setTile(id, show){ if(show) hiddenTiles.delete(id); else hiddenTiles.add(id);
  applyTile(id); saveHidden(hiddenTiles); renderDock(); }
function toggleTile(id){ setTile(id, hiddenTiles.has(id)); }   // hidden -> show
function renderDock(){
  const d=document.getElementById('dock'); if(!d) return;
  const tileChips=TILES.map(([id,label])=>
    `<span class="dchip ${hiddenTiles.has(id)?'':'on'}" data-tile="${id}">${label}</span>`).join('');
  // voice is on-demand (not a persistent tile) but lives in the dock so it's openable/closeable here
  const vOn=$('voiceTile')&&$('voiceTile').classList.contains('show');
  const voiceChip=`<span class="dchip ${vOn?'on':''}" data-voice="1">voice</span>`;
  const lOn=$('lensPanel')&&$('lensPanel').classList.contains('show');
  const lensChip=`<span class="dchip ${lOn?'on':''}" data-lens="1">lens</span>`;
  const scOn=$('scopePanel')&&$('scopePanel').classList.contains('show');
  const scopeChip=`<span class="dchip ${scOn?'on':''}" data-scope="1">scope</span>`;
  // #60: search (find) is closeable too, so it gets a dock chip to re-open it.
  const seOn=$('search')&&!$('search').classList.contains('search-off');
  const searchChip=`<span class="dchip ${seOn?'on':''}" data-search="1">find</span>`;
  d.innerHTML='<span class="dlabel">tiles</span>'+tileChips+voiceChip+lensChip+scopeChip+searchChip;
  d.querySelectorAll('[data-tile]').forEach(c=>c.onclick=()=>toggleTile(c.dataset.tile));
  const vc=d.querySelector('[data-voice]'); if(vc) vc.onclick=()=>toggleVoice();
  const lc=d.querySelector('[data-lens]'); if(lc) lc.onclick=()=>toggleLens();
  const sc=d.querySelector('[data-scope]'); if(sc) sc.onclick=()=>toggleScopePanel();
  const se=d.querySelector('[data-search]'); if(se) se.onclick=()=>{ const s=$('search'); if(s){ s.classList.toggle('search-off'); renderDock(); } };
}
// inject a hover ✕ into each tile and apply any saved hidden state
TILES.forEach(([id])=>{
  const el=document.getElementById(id); if(!el) return;
  const x=document.createElement('span'); x.className='tile-x'; x.textContent='✕';
  x.title='hide this tile (re-open it from the dock)';
  x.onclick=ev=>{ ev.stopPropagation(); setTile(id, false); };
  el.appendChild(x); applyTile(id);
});
// #60: the toggle-panels (lens, scope, search) aren't in TILES but must ALSO have
// a visible ✕ so every widget is closeable. They hide via their own toggle; the
// dock chips (lens / scope / find) re-open them.
[['lensPanel',()=>toggleLens(false)],
 ['scopePanel',()=>toggleScopePanel(false)],
 ['search',()=>{ const s=$('search'); if(s){ s.classList.add('search-off'); renderDock(); } }]
].forEach(([id,close])=>{
  const el=document.getElementById(id); if(!el || el.querySelector('.tile-x')) return;
  const x=document.createElement('span'); x.className='tile-x'; x.textContent='✕'; x.title='close (re-open from the dock)';
  x.onclick=ev=>{ ev.stopPropagation(); close(); };
  el.appendChild(x);
});
renderDock();

// double-click the title to reset the whole layout
document.getElementById('title').addEventListener('dblclick',()=>{
  try{localStorage.removeItem('orbPanels');localStorage.removeItem('orbSections');localStorage.removeItem('orbHidden');localStorage.removeItem('orbLens');}catch(e){} location.reload(); });

/* ============================ main loop ============================ */
// Global motion scale: BASE_SPEED is the resting pace (15% slower than 1.0).
// Zooming freezes motion; after FREEZE_IDLE_S of no zoom it slowly ramps back.
const BASE_SPEED=0.64, FREEZE_IDLE_S=20;   // global animation pace (0.64 ≈ 25% slower than 0.85)
let motionSpeed=BASE_SPEED;     // current animation speed (0 = frozen)
let mt=0;                       // motion clock — drives every oscillation, freezes with motionSpeed
let lastZoom=-1e9;              // timestamp (s) of the last zoom interaction
let last=performance.now();
function animate(now){
  requestAnimationFrame(animate);
  const dt=Math.min(0.05,(now-last)/1000); last=now;
  const t=now/1000;

  // ease the global motion speed: freeze fairly quickly on zoom, resume slowly after idle
  const target = (t-lastZoom) >= FREEZE_IDLE_S ? BASE_SPEED : 0;
  const rate = target > motionSpeed ? 0.28 : 2.5;     // resume slow, freeze quicker
  motionSpeed += (target-motionSpeed)*Math.min(1,dt*rate);
  if(motionSpeed<0.0005) motionSpeed=0;
  mt += dt*motionSpeed;                                // all ambient animation runs on this clock
  nodeUniforms.uTime.value=mt; membraneUniforms.uTime.value=mt;

  updateAuto(dt);
  updateAmbient(dt, t);
  const S=STATES[state];
  // smooth state params (eased gently so transitions don't snap)
  cur.rot   += (S.rot-cur.rot)*Math.min(1,dt*1.6);
  cur.breath+= (S.breath-cur.breath)*Math.min(1,dt*1.6);
  cur.tintAmt+= ((S.tintAmt||0)-cur.tintAmt)*Math.min(1,dt*1.6);
  cur.tint.lerp(new THREE.Color(S.tint), Math.min(1,dt*1.6));
  const targetScale=S.scale||1;
  nodeUniforms.uTint.value.copy(cur.tint);
  nodeUniforms.uTintAmt.value=cur.tintAmt;

  // rotation + breathing (quaternion; fly-to overrides auto-spin)
  if(focusing){
    root.quaternion.slerp(qFocus, Math.min(1,dt*2.4));
    if(root.quaternion.angleTo(qFocus)<0.015) focusing=false;
  } else if(!dragging){
    // organic drift: the spin waxes and wanes, with a faint independent tumble.
    // Scaled by motionSpeed, so it stops when frozen and resumes when it ramps back.
    // primary horizontal orbit, with a fuller vertical arc whose axis slowly precesses,
    // so the view circles the orb from changing angles rather than spinning on one axis
    const yawRate   = cur.rot * (0.85 + 0.25*Math.sin(mt*0.05 + 0.6)) * motionSpeed;
    const pitchRate = cur.rot * (0.40*Math.sin(mt*0.037 + 1.7) + 0.16*Math.sin(mt*0.011)) * motionSpeed;
    rotateWorld(pitchRate*dt, yawRate*dt);
  }
  motes.rotation.y -= 0.006*dt*motionSpeed; motes.rotation.x += 0.0022*dt*motionSpeed;
  // breathing: two slow waves summed -> gentle, irregular, never mechanical
  let breathe=1+(Math.sin(mt*0.42)*0.6 + Math.sin(mt*0.19+1.3)*0.4)*cur.breath;
  // listening ripple / speaking cadence / hungry contract (all slowed)
  let heatBoost=0;
  if(state==='listening'){ const amp=Math.abs(Math.sin(mt*3.3)*0.6+Math.sin(mt*5.7)*0.4); breathe+=amp*0.015; heatBoost=amp*0.35; }
  if(state==='speaking'){ const c=Math.max(0,Math.sin(mt*2.8)); heatBoost=c*0.6; breathe+=c*0.010; }
  if(state==='hungry'){ if(Math.random()<0.4*motionSpeed) spawnParticle(true); }
  if(state==='ingesting'){ if(Math.random()<0.5*motionSpeed) spawnParticle(false); }
  if(ambientGlow>0) heatBoost=Math.max(heatBoost, ambientGlow);   // ambient soft bloom
  nodeUniforms.uHeatBoost.value += (heatBoost-nodeUniforms.uHeatBoost.value)*Math.min(1,dt*3);
  const gs=root.scale.x + (targetScale-root.scale.x)*Math.min(1,dt*1.4);
  root.scale.setScalar(gs*breathe);

  updateWave(dt);
  updateParticles(dt);
  updateEndorsing(dt);

  // tension pulse (immune to everything) — slow, breathing red
  if(tensionLines) tensionLines.material.opacity=0.42+0.30*Math.abs(Math.sin(mt*0.9));

  // incubation cells: pulse, converged brighter, hatch bloom (slowed, varied by phase)
  incCells.forEach(c=>{
    const u=c.userData; let s=u.baseScale;
    s*= 1+0.06*Math.sin(mt*0.6+u.phase);
    if(u.converged) c.material.opacity=0.82+0.18*Math.sin(mt*1.1+u.phase);
    if(u.hatch){ s*=1+u.hatch; u.hatch=Math.max(0,u.hatch-dt*0.9); }
    if(!u.endorsed) c.scale.set(s,s,1);
  });

  // camera dolly (fly inside) — never frozen, so zoom always completes
  camDist+=(camTargetDist-camDist)*Math.min(1,dt*4);
  camera.position.z=camDist;
  updateLabels();
  updateLiveNotes();   // pulse + reposition labels for session-captured notes

  renderer.render(scene,camera);
}
// lightweight introspection (handy for debugging the motion/freeze behaviour)
window.orbState=()=>({motionSpeed:+motionSpeed.toFixed(3), frozen:motionSpeed<0.05,
  sinceZoom:+(performance.now()/1000-lastZoom).toFixed(1),
  camDist:+camDist.toFixed(1), camTargetDist:+camTargetDist.toFixed(1), focusing});
window.orbHover=()=>({i:hoverIndex, labels:hoverSet?hoverSet.length:0, lines:!!(hoverLines&&hoverLines.visible)});
window.nodeScreen=(i)=>{ const v=NODES[i]._pos.clone().applyMatrix4(root.matrixWorld).project(camera);
  return {x:(v.x*0.5+0.5)*innerWidth, y:(-v.y*0.5+0.5)*innerHeight, front:v.z<1}; };

/* ---- keyboard focus: an auto-opened tab may not hold focus, so the first
   keystrokes get lost. Grab focus on load and keep it on interaction. ---- */
function grabFocus(){ try{ canvas.focus({preventScroll:true}); }catch(e){} }
addEventListener('focus', grabFocus);
addEventListener('pointerdown', e=>{
  // keep keyboard alive after clicks, but don't fight text selection in the inspector/reading
  if(!(e.target.closest && e.target.closest('#search,#inspContent,#readBody,#tenBody,input,textarea'))) grabFocus();
}, true);

/* ============================ boot ============================ */
loadData().then(d=>{
  buildGraph(d);
  document.getElementById('loading').style.display='none';
  grabFocus();
  initActions();   // detect the action backend (proxy) and enable capture/connect/run-skill if present
  loadScopes();    // populate the live mount-set (for the scope panel + voice scope tools)
  // mirror the agent's most recent real activity, then settle to idle
  const a=(d.activity||[])[0];
  if(a && STATES[a.state]){
    setState(a.state);
    $('stateDesc').textContent=`reflecting last run · ${a.kind} ${a.ago}`;
    setTimeout(()=>setState('idle'),6500);
  } else setState('idle');
  requestAnimationFrame(animate);
}).catch(err=>{
  document.getElementById('loading').textContent="this agent hasn't rendered its mind yet";
  console.error(err);
  try { if (window.parent && window.parent!==window) window.parent.postMessage({type:'brain-orb:error', message:String((err&&err.message)||err)}, window.location.origin); } catch(_){}
});
