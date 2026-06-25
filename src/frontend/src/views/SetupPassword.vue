<template>
  <!-- First-time setup — a deliberately dark, branded welcome hero (pre-login,
       no theme preference exists yet). trinity-enterprise#49: setup-token field
       removed, admin email required, welcoming animated first-run page. -->
  <div class="setup-root">
    <div class="aurora" aria-hidden="true"></div>
    <div class="grid-fade" aria-hidden="true"></div>

    <div class="stage">
      <!-- LEFT: welcome + orbiting fleet constellation -->
      <section class="hero">
        <div class="constellation" aria-hidden="true">
          <div class="halo"></div>
          <div class="core">
            <svg viewBox="0 0 1024 1024"><g transform="translate(0,1024) scale(0.1,-0.1)">
              <path d="M5043 8108 c-218 -17 -433 -98 -618 -233 -68 -50 -105 -81 -105 -88 0 -2 37 -8 83 -11 380 -32 781 -219 1017 -473 124 -134 234 -321 289 -492 72 -221 89 -533 45 -810 l-6 -36 129 -18 c161 -22 342 -64 503 -116 69 -23 127 -41 130 -41 4 0 11 33 18 73 74 456 39 908 -98 1274 -245 651 -771 1019 -1387 971z"/>
              <path d="M4109 7564 c-110 -155 -171 -277 -232 -469 -75 -235 -102 -418 -101 -695 0 -356 48 -646 170 -1023 106 -327 336 -774 569 -1107 84 -120 99 -140 104 -139 3 0 47 52 96 115 113 146 261 370 322 489 l46 91 -93 163 c-166 291 -288 583 -360 866 -116 455 -94 869 63 1180 64 127 180 270 288 352 27 20 49 40 49 44 0 14 -198 108 -292 138 -141 45 -253 63 -423 68 l-150 5 -56 -78z"/>
              <path d="M4955 5828 c-82 -5 -151 -10 -152 -12 -6 -6 68 -227 106 -317 59 -140 135 -300 180 -377 l40 -69 168 -7 c717 -31 1291 -239 1682 -610 225 -214 355 -483 387 -799 l6 -58 66 36 c237 130 454 332 564 525 l26 45 -29 85 c-85 253 -261 517 -494 739 -511 487 -1232 775 -2060 821 -195 11 -281 11 -490 -2z"/>
              <path d="M3629 5507 c-411 -186 -765 -449 -1002 -746 -226 -283 -348 -581 -363 -886 -31 -598 342 -1102 936 -1265 41 -11 85 -20 97 -20 21 0 20 4 -22 59 -220 289 -333 675 -296 1014 31 288 155 533 378 747 145 140 264 222 471 326 94 48 172 88 172 90 0 1 -18 42 -41 91 -82 177 -152 365 -209 561 -11 39 -22 72 -23 72 -1 0 -45 -20 -98 -43z"/>
              <path d="M5208 4737 c-166 -329 -359 -598 -602 -843 -297 -298 -562 -459 -871 -529 -195 -44 -347 -42 -539 6 -43 11 -79 19 -81 17 -1 -2 6 -43 16 -93 49 -237 164 -471 325 -659 l59 -69 94 2 c414 10 892 207 1313 543 217 172 514 498 703 772 152 219 459 781 467 856 3 20 -4 25 -57 41 -164 50 -536 109 -685 109 l-65 0 -77 -153z"/>
              <path d="M8006 3911 c-123 -150 -298 -296 -474 -395 -126 -71 -228 -112 -366 -144 -98 -23 -132 -26 -291 -26 -159 0 -193 3 -291 26 -244 57 -470 177 -700 371 l-100 85 -105 -141 c-113 -153 -220 -279 -337 -399 -42 -43 -74 -79 -71 -82 51 -48 227 -180 336 -253 244 -162 517 -281 773 -337 542 -119 1016 4 1351 351 166 172 260 344 320 587 23 91 37 365 22 404 -9 23 -12 21 -67 -47z"/>
            </g></svg>
          </div>
          <div class="orbit o1"><span class="node indigo n-top"></span></div>
          <div class="orbit o2"><span class="node purple n-top"></span><span class="node green n-bottom"></span></div>
          <div class="orbit o3"><span class="node green n-left"></span><span class="node indigo n-right"></span></div>
        </div>

        <div class="eyebrow">Trinity · First-time setup</div>
        <h1>Welcome to <span class="grad">Trinity</span></h1>
        <p class="lede">Sovereign infrastructure for your fleet of autonomous agents. Let's create your admin account.</p>
        <div class="props">
          <span><i></i> Governed</span>
          <span><i></i> Auditable</span>
          <span><i></i> Your infrastructure</span>
        </div>
      </section>

      <!-- RIGHT: setup form -->
      <section class="formwrap">
        <div class="card">
          <h2>Create your admin account</h2>
          <p class="sub">This sets up the owner of this Trinity instance.</p>

          <form @submit.prevent="handleSubmit" novalidate>
            <!-- Admin email (required) -->
            <div class="field">
              <label for="adminEmail">Admin email <span class="req">*</span></label>
              <div class="ipt">
                <input
                  type="email"
                  id="adminEmail"
                  v-model="email"
                  :disabled="loading"
                  placeholder="you@company.com"
                  autocomplete="email"
                  required
                />
              </div>
              <p class="hint">You'll sign in with this email and your password.</p>
            </div>

            <!-- Password -->
            <div class="field">
              <label for="password">Password <span class="req">*</span></label>
              <div class="ipt">
                <input
                  :type="showPassword ? 'text' : 'password'"
                  id="password"
                  v-model="password"
                  :disabled="loading"
                  placeholder="Enter password (12+ characters)"
                  autocomplete="new-password"
                  required
                  minlength="12"
                />
                <span class="eye" role="button" tabindex="0" @click="showPassword = !showPassword" @keyup.enter="showPassword = !showPassword">
                  <svg v-if="showPassword" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-width="2" stroke-linecap="round" stroke-linejoin="round" d="M13.875 18.825A10.05 10.05 0 0112 19c-4.478 0-8.268-2.943-9.543-7a9.97 9.97 0 011.563-3.029m5.858.908a3 3 0 114.243 4.243M9.878 9.878l4.242 4.242M9.88 9.88l-3.29-3.29m7.532 7.532l3.29 3.29M3 3l3.59 3.59m0 0A9.953 9.953 0 0112 5c4.478 0 8.268 2.943 9.543 7a10.025 10.025 0 01-4.132 5.411m0 0L21 21"/></svg>
                  <svg v-else fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-width="2" stroke-linecap="round" stroke-linejoin="round" d="M15 12a3 3 0 11-6 0 3 3 0 016 0z"/><path stroke-width="2" stroke-linecap="round" stroke-linejoin="round" d="M2.458 12C3.732 7.943 7.523 5 12 5c4.478 0 8.268 2.943 9.542 7-1.274 4.057-5.064 7-9.542 7-4.477 0-8.268-2.943-9.542-7z"/></svg>
                </span>
              </div>

              <template v-if="password">
                <div class="meter"><i :style="{ width: `${passwordStrength * 20}%`, background: strengthColor }"></i></div>
                <div class="strength"><span>Strength</span><b :style="{ color: strengthColor }">{{ passwordStrengthText }}</b></div>
                <div class="reqs">
                  <div v-for="req in passwordRequirements" :key="req.label" :class="{ met: req.met }">
                    <svg v-if="req.met" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-width="3" stroke-linecap="round" stroke-linejoin="round" d="M5 13l4 4L19 7"/></svg>
                    <svg v-else fill="none" stroke="currentColor" viewBox="0 0 24 24"><circle cx="12" cy="12" r="9" stroke-width="2"/></svg>
                    {{ req.label }}
                  </div>
                </div>
              </template>
            </div>

            <!-- Confirm password -->
            <div class="field">
              <label for="confirmPassword">Confirm password <span class="req">*</span></label>
              <div class="ipt">
                <input
                  :type="showConfirmPassword ? 'text' : 'password'"
                  id="confirmPassword"
                  v-model="confirmPassword"
                  :disabled="loading"
                  placeholder="Confirm your password"
                  autocomplete="new-password"
                  required
                  minlength="12"
                />
                <span class="eye" role="button" tabindex="0" @click="showConfirmPassword = !showConfirmPassword" @keyup.enter="showConfirmPassword = !showConfirmPassword">
                  <svg v-if="showConfirmPassword" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-width="2" stroke-linecap="round" stroke-linejoin="round" d="M13.875 18.825A10.05 10.05 0 0112 19c-4.478 0-8.268-2.943-9.543-7a9.97 9.97 0 011.563-3.029m5.858.908a3 3 0 114.243 4.243M9.878 9.878l4.242 4.242M9.88 9.88l-3.29-3.29m7.532 7.532l3.29 3.29M3 3l3.59 3.59m0 0A9.953 9.953 0 0112 5c4.478 0 8.268 2.943 9.543 7a10.025 10.025 0 01-4.132 5.411m0 0L21 21"/></svg>
                  <svg v-else fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-width="2" stroke-linecap="round" stroke-linejoin="round" d="M15 12a3 3 0 11-6 0 3 3 0 016 0z"/><path stroke-width="2" stroke-linecap="round" stroke-linejoin="round" d="M2.458 12C3.732 7.943 7.523 5 12 5c4.478 0 8.268 2.943 9.542 7-1.274 4.057-5.064 7-9.542 7-4.477 0-8.268-2.943-9.542-7z"/></svg>
                </span>
              </div>
              <div v-if="password && confirmPassword" class="match" :class="{ bad: !passwordsMatch }">
                <svg v-if="passwordsMatch" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" d="M5 13l4 4L19 7"/></svg>
                <svg v-else fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" d="M6 18L18 6M6 6l12 12"/></svg>
                {{ passwordsMatch ? 'Passwords match' : 'Passwords do not match' }}
              </div>
            </div>

            <div class="divider"></div>

            <!-- Company / organization (optional) -->
            <div class="field">
              <label for="company">Company / organization <span class="opt">(optional)</span></label>
              <div class="ipt">
                <input type="text" id="company" v-model="company" :disabled="loading" placeholder="Acme Inc." autocomplete="organization" />
              </div>
            </div>

            <!-- Updates opt-in -->
            <div class="checkrow">
              <input id="consentUpdates" type="checkbox" v-model="consentUpdates" :disabled="loading" />
              <label for="consentUpdates">Occasionally email me important security &amp; product updates.
                <small>Sends your email{{ company ? ' + company' : '' }} to ability.ai — nothing else. Skippable; disable via env on air-gapped installs.</small>
              </label>
            </div>

            <!-- Error -->
            <div v-if="error" class="errbox">
              <svg fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-width="2" stroke-linecap="round" stroke-linejoin="round" d="M12 8v4m0 4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z"/></svg>
              <span>{{ error }}</span>
            </div>

            <button class="cta" type="submit" :disabled="!isValid || loading">
              <svg v-if="loading" class="spin" fill="none" viewBox="0 0 24 24"><circle class="o25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle><path class="o75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z"></path></svg>
              {{ loading ? 'Creating account…' : 'Create admin account & continue →' }}
            </button>

            <div class="secnote">
              <svg fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-width="2" stroke-linecap="round" stroke-linejoin="round" d="M12 15v2m-6 4h12a2 2 0 002-2v-6a2 2 0 00-2-2H6a2 2 0 00-2 2v6a2 2 0 002 2zm10-10V7a4 4 0 00-8 0v4h8z"/></svg>
              Available only until your admin account is created.
            </div>
            <p class="footnote">Prefer the classic login? Sign in as <code>admin</code> with this password anytime.</p>
          </form>
        </div>
      </section>
    </div>
  </div>
</template>

<script setup>
import { ref, computed } from 'vue'
import { useRouter } from 'vue-router'
import axios from 'axios'
import { clearSetupCache } from '../router'

const router = useRouter()

const email = ref('')
const password = ref('')
const confirmPassword = ref('')
const company = ref('')
const consentUpdates = ref(false)
const showPassword = ref(false)
const showConfirmPassword = ref(false)
const loading = ref(false)
const error = ref(null)

// Mirror the backend _EMAIL_RE shape check (one @, a dot in the domain, no spaces).
const EMAIL_RE = /^[^@\s]+@[^@\s]+\.[^@\s.]+$/
const emailValid = computed(() => EMAIL_RE.test(email.value.trim()))

const passwordsMatch = computed(() => password.value === confirmPassword.value)

const passwordRequirements = computed(() => {
  const p = password.value
  return [
    { label: 'At least 12 characters', met: p.length >= 12 },
    { label: 'Uppercase letter (A-Z)', met: /[A-Z]/.test(p) },
    { label: 'Lowercase letter (a-z)', met: /[a-z]/.test(p) },
    { label: 'Number (0-9)', met: /[0-9]/.test(p) },
    { label: 'Special character (!@#$…)', met: /[^A-Za-z0-9]/.test(p) },
  ]
})

const passwordStrength = computed(() => passwordRequirements.value.filter(r => r.met).length)

const passwordStrengthText = computed(() => {
  const texts = ['Very Weak', 'Weak', 'Fair', 'Good', 'Strong', 'Excellent']
  return texts[passwordStrength.value] || 'Very Weak'
})

const strengthColor = computed(() => {
  const colors = ['#f87171', '#f87171', '#fb923c', '#fbbf24', '#4ade80', '#4ade80']
  return colors[passwordStrength.value] || colors[0]
})

const isValid = computed(() =>
  emailValid.value && passwordRequirements.value.every(r => r.met) && passwordsMatch.value
)

async function handleSubmit() {
  if (!isValid.value) return

  loading.value = true
  error.value = null

  try {
    await axios.post('/api/setup/admin-password', {
      email: email.value.trim(),
      password: password.value,
      confirm_password: confirmPassword.value,
      company: company.value.trim() || null,
      consent_updates: consentUpdates.value,
    })

    // Clear the cache so the router knows setup is done, then go to login.
    clearSetupCache()
    router.push('/login')
  } catch (e) {
    if (e.response?.status === 403) {
      // Setup already completed — endpoint self-disabled. Send them to login.
      error.value = 'Setup has already been completed.'
      setTimeout(() => router.push('/login'), 2000)
    } else {
      error.value = e.response?.data?.detail || 'Failed to create the admin account. Please try again.'
    }
  } finally {
    loading.value = false
  }
}
</script>

<style scoped>
.setup-root{
  --indigo-400:#818cf8; --indigo-500:#6366f1; --indigo-600:#4f46e5;
  --purple-400:#c084fc; --purple-500:#a855f7;
  --green-400:#4ade80; --green-500:#22c55e;
  --g950:#080a12; --g900:#0f1320; --g800:#1b2233; --g700:#2a3142;
  --ink:#f3f5fb; --muted:#9aa3b8; --muted2:#6b7385;
  /* Normal-flow full-bleed (NOT position:fixed) — fixed roots get clipped to a
     transformed/contained ancestor and leave the viewport's light #app bg
     showing through. width:100% fills the full-width #app; min-height:100vh
     keeps the dark canvas covering the screen even when the form is short. */
  position:relative; width:100%; min-height:100vh; overflow-x:hidden;
  background:var(--g950); color:var(--ink);
  font-family:'Inter',ui-sans-serif,system-ui,-apple-system,'Segoe UI',Roboto,sans-serif;
  -webkit-font-smoothing:antialiased;
}

/* ambient aurora backdrop */
.aurora{position:absolute;inset:0;z-index:0;overflow:hidden;pointer-events:none}
.aurora::before,.aurora::after{content:"";position:absolute;border-radius:50%;filter:blur(90px);opacity:.5}
.aurora::before{width:780px;height:780px;left:-180px;top:-220px;
  background:radial-gradient(circle at center,rgba(79,70,229,.55),transparent 60%);
  animation:drift1 22s ease-in-out infinite alternate}
.aurora::after{width:680px;height:680px;right:-160px;bottom:-200px;
  background:radial-gradient(circle at center,rgba(168,85,247,.42),transparent 60%);
  animation:drift2 26s ease-in-out infinite alternate}
@keyframes drift1{from{transform:translate(0,0)}to{transform:translate(60px,40px)}}
@keyframes drift2{from{transform:translate(0,0)}to{transform:translate(-50px,-30px)}}
.grid-fade{position:absolute;inset:0;z-index:0;opacity:.18;pointer-events:none;
  background-image:linear-gradient(rgba(129,140,248,.10) 1px,transparent 1px),
                   linear-gradient(90deg,rgba(129,140,248,.10) 1px,transparent 1px);
  background-size:48px 48px;
  -webkit-mask-image:radial-gradient(circle at 30% 40%,black,transparent 75%);
  mask-image:radial-gradient(circle at 30% 40%,black,transparent 75%)}

.stage{position:relative;z-index:1;min-height:100vh;display:grid;grid-template-columns:1fr 1fr}
@media(max-width:900px){.stage{grid-template-columns:1fr}}

/* LEFT hero */
.hero{display:flex;flex-direction:column;justify-content:center;align-items:center;padding:48px 56px;text-align:center;gap:8px}
@media(max-width:900px){.hero{padding:40px 24px 8px}}
.constellation{position:relative;width:340px;height:340px;margin-bottom:14px}
@media(max-width:900px){.constellation{width:220px;height:220px}}
.orbit{position:absolute;top:50%;left:50%;border-radius:50%;border:1px solid rgba(129,140,248,.16);transform:translate(-50%,-50%);transform-origin:center}
.o1{width:150px;height:150px;animation:spin 16s linear infinite}
.o2{width:236px;height:236px;animation:spin 26s linear infinite reverse;border-style:dashed;border-color:rgba(168,85,247,.14)}
.o3{width:322px;height:322px;animation:spin 40s linear infinite}
@media(max-width:900px){.o1{width:100px;height:100px}.o2{width:155px;height:155px}.o3{width:212px;height:212px}}
@keyframes spin{to{transform:translate(-50%,-50%) rotate(360deg)}}
.node{position:absolute;width:13px;height:13px;border-radius:50%;left:50%;margin-left:-6.5px;animation:pulse 3.2s ease-in-out infinite}
.node::after{content:"";position:absolute;inset:-5px;border-radius:50%;opacity:.5;filter:blur(4px)}
.n-top{top:-6.5px}.n-bottom{bottom:-6.5px}
.n-left{top:50%;left:-6.5px;margin:-6.5px 0 0 0}
.n-right{top:50%;left:auto;right:-6.5px;margin:-6.5px 0 0 0}
.indigo{background:var(--indigo-400);box-shadow:0 0 12px var(--indigo-500)}.indigo::after{background:var(--indigo-500)}
.purple{background:var(--purple-400);box-shadow:0 0 12px var(--purple-500)}.purple::after{background:var(--purple-500)}
.green{background:var(--green-400);box-shadow:0 0 12px var(--green-500)}.green::after{background:var(--green-500)}
@keyframes pulse{0%,100%{opacity:.55;transform:scale(.85)}50%{opacity:1;transform:scale(1.15)}}
.core{position:absolute;top:50%;left:50%;width:78px;height:78px;margin:-39px 0 0 -39px;border-radius:50%;display:flex;align-items:center;justify-content:center;
  background:radial-gradient(circle at 35% 30%,#3730a3,#1e1b4b 70%);
  box-shadow:0 0 0 1px rgba(129,140,248,.35),0 0 40px rgba(99,102,241,.55),inset 0 0 18px rgba(168,85,247,.4);
  animation:corepulse 4s ease-in-out infinite}
@media(max-width:900px){.core{width:56px;height:56px;margin:-28px 0 0 -28px}}
@keyframes corepulse{0%,100%{box-shadow:0 0 0 1px rgba(129,140,248,.35),0 0 36px rgba(99,102,241,.5),inset 0 0 18px rgba(168,85,247,.4)}50%{box-shadow:0 0 0 1px rgba(129,140,248,.5),0 0 60px rgba(99,102,241,.8),inset 0 0 22px rgba(168,85,247,.55)}}
.core svg{width:42px;height:42px;fill:#e0e7ff;filter:drop-shadow(0 0 6px rgba(165,180,252,.8))}
@media(max-width:900px){.core svg{width:30px;height:30px}}
.halo{position:absolute;top:50%;left:50%;width:78px;height:78px;margin:-39px 0 0 -39px;border-radius:50%;border:1px solid rgba(129,140,248,.5);animation:halo 3.6s ease-out infinite}
@keyframes halo{0%{opacity:.6;transform:scale(1)}100%{opacity:0;transform:scale(2.6)}}
.eyebrow{font-size:12px;letter-spacing:.22em;text-transform:uppercase;color:var(--indigo-400);font-weight:600}
.hero h1{font-size:34px;line-height:1.1;margin:6px 0 4px;font-weight:700;letter-spacing:-.02em}
.hero h1 .grad{background:linear-gradient(90deg,#a5b4fc,#c084fc);-webkit-background-clip:text;background-clip:text;color:transparent}
.hero p.lede{color:var(--muted);font-size:15px;max-width:380px;margin:2px auto 18px;line-height:1.5}
.props{display:flex;gap:18px;flex-wrap:wrap;justify-content:center;color:var(--muted2);font-size:12.5px}
.props span{display:inline-flex;align-items:center;gap:6px}
.props i{width:5px;height:5px;border-radius:50%;background:var(--green-400);box-shadow:0 0 8px var(--green-500)}
@media(max-width:900px){.hero h1{font-size:26px}.props{display:none}}

/* RIGHT form */
.formwrap{display:flex;align-items:center;justify-content:center;padding:40px 56px}
@media(max-width:900px){.formwrap{padding:8px 22px 40px}}
.card{width:100%;max-width:404px;background:rgba(21,26,41,.72);backdrop-filter:blur(14px);border:1px solid rgba(129,140,248,.14);border-radius:18px;padding:30px 30px 26px;box-shadow:0 24px 60px -20px rgba(0,0,0,.7)}
.card h2{margin:0 0 4px;font-size:19px;font-weight:650}
.card .sub{margin:0 0 20px;color:var(--muted);font-size:13px}
.field{margin-bottom:14px}
.field label{display:block;font-size:12.5px;font-weight:550;color:#cbd2e3;margin-bottom:6px}
.field label .req{color:var(--indigo-400)}
.field label .opt{color:var(--muted2);font-weight:400}
.ipt{position:relative}
.ipt input{width:100%;background:var(--g900);border:1px solid var(--g700);border-radius:10px;padding:10px 12px;color:var(--ink);font-size:14px;outline:none;transition:border .15s,box-shadow .15s}
.ipt input::placeholder{color:var(--muted2)}
.ipt input:focus{border-color:var(--indigo-500);box-shadow:0 0 0 3px rgba(99,102,241,.25)}
.ipt input:disabled{opacity:.6}
.ipt .eye{position:absolute;right:10px;top:50%;transform:translateY(-50%);color:var(--muted2);cursor:pointer;display:flex}
.ipt .eye svg{width:18px;height:18px}
.hint{font-size:11.5px;color:var(--muted2);margin-top:5px}
.meter{height:4px;border-radius:3px;background:var(--g700);margin-top:8px;overflow:hidden}
.meter>i{display:block;height:100%;border-radius:3px;transition:width .3s,background .3s}
.strength{display:flex;justify-content:space-between;font-size:11.5px;margin-top:6px;color:var(--muted)}
.strength b{font-weight:600}
.reqs{margin:8px 0 2px;display:grid;grid-template-columns:1fr 1fr;gap:3px 12px}
.reqs div{display:flex;align-items:center;gap:6px;font-size:11.5px;color:var(--muted2)}
.reqs div.met{color:var(--green-400)}
.reqs svg{width:13px;height:13px;flex:none}
.match{display:flex;align-items:center;gap:6px;font-size:12px;color:var(--green-400);margin-top:7px}
.match.bad{color:#f87171}
.match svg{width:14px;height:14px}
.divider{height:1px;background:var(--g700);margin:18px 0 16px;opacity:.7}
.checkrow{display:flex;gap:10px;align-items:flex-start;margin-top:4px}
.checkrow input{margin-top:2px;width:15px;height:15px;accent-color:var(--indigo-600);flex:none}
.checkrow label{font-size:12.5px;color:var(--muted);line-height:1.45}
.checkrow label small{display:block;color:var(--muted2);font-size:11px;margin-top:2px}
.errbox{display:flex;gap:8px;align-items:flex-start;margin-top:16px;padding:10px 12px;border-radius:10px;background:rgba(248,113,113,.1);border:1px solid rgba(248,113,113,.3);color:#fca5a5;font-size:12.5px}
.errbox svg{width:16px;height:16px;flex:none;margin-top:1px}
.cta{width:100%;margin-top:20px;border:0;border-radius:11px;padding:12px;font-size:14px;font-weight:600;color:#fff;cursor:pointer;display:flex;align-items:center;justify-content:center;gap:8px;
  background:linear-gradient(180deg,var(--indigo-500),var(--indigo-600));box-shadow:0 8px 22px -8px rgba(79,70,229,.8);transition:filter .15s}
.cta:hover:not(:disabled){filter:brightness(1.08)}
.cta:disabled{opacity:.5;cursor:not-allowed}
.cta .spin{width:18px;height:18px;animation:rot .8s linear infinite}
.cta .spin .o25{opacity:.25}.cta .spin .o75{opacity:.75}
@keyframes rot{to{transform:rotate(360deg)}}
.secnote{display:flex;gap:8px;align-items:center;justify-content:center;margin-top:12px;font-size:11px;color:var(--muted2)}
.secnote svg{width:13px;height:13px;color:var(--indigo-400);flex:none}
.footnote{text-align:center;font-size:11.5px;color:var(--muted2);margin-top:10px;line-height:1.5}
.footnote code{background:var(--g800);padding:1px 5px;border-radius:4px;color:#cbd2e3;font-size:11px}

/* Respect reduced-motion: freeze the constellation into a composed static state. */
@media (prefers-reduced-motion: reduce){
  .aurora::before,.aurora::after,.o1,.o2,.o3,.node,.core,.halo,.cta .spin{animation:none}
  .halo{opacity:0}
}
</style>
