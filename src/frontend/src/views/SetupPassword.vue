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
            <TrinityMark />
          </div>
          <div class="orbit o1"><span class="node indigo n-top"></span></div>
          <div class="orbit o2"><span class="node purple n-top"></span><span class="node green n-bottom"></span></div>
          <div class="orbit o3"><span class="node green n-left"></span><span class="node indigo n-right"></span></div>
        </div>

        <div class="eyebrow">Trinity · First-time setup</div>
        <h1>Welcome to <span class="grad">Trinity</span></h1>
        <p class="lede">The operating system for the AI-native company — open source, self-hosted, that you own. Let's create your admin account.</p>
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
          <p class="sub">{{ linkMode
            ? 'Choose a password to finish setting up your instance.'
            : 'This sets up the owner of this Trinity instance.' }}</p>

          <!-- trinity-enterprise#1312: a setup link that cannot be used names
               the next step instead of showing a form that would be refused. -->
          <div v-if="linkProblem" class="linkproblem" data-testid="setup-link-problem" role="alert">
            <svg fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-width="2" stroke-linecap="round" stroke-linejoin="round" d="M3 8l7.89 5.26a2 2 0 002.22 0L21 8M5 19h14a2 2 0 002-2V7a2 2 0 00-2-2H5a2 2 0 00-2 2v10a2 2 0 002 2z"/></svg>
            <span>{{ LINK_PROBLEM_TEXT[linkProblem] }}</span>
          </div>

          <form v-else @submit.prevent="handleSubmit" novalidate>
            <!-- EC2 instance ID (#3004): an AWS install's first admin proves
                 they control this server. An emailed setup link carries its
                 code instead (trinity-enterprise#1312). -->
            <div v-if="claimRequired && !linkMode" class="field">
              <div class="labrow">
                <label for="instanceId">EC2 instance ID <span class="req">*</span></label>
                <span v-if="claimCode.trim() && !claimValid" class="match bad">Format: i-0abc123…</span>
              </div>
              <div class="ipt">
                <input
                  type="text"
                  id="instanceId"
                  v-model="claimCode"
                  :disabled="loading"
                  placeholder="i-0abc123def4567890"
                  autocomplete="off"
                  autocapitalize="off"
                  spellcheck="false"
                  required
                />
              </div>
              <p class="hint">
                EC2 console → Instances → Instance ID.
                <a :href="EC2_INSTANCES_URL" target="_blank" rel="noopener noreferrer">Open EC2</a>
              </p>
            </div>

            <!-- Admin email (required; optional on an instance-ID claim, #3004) -->
            <div class="field">
              <label for="adminEmail">Admin email <span v-if="claimRequired && !linkMode" class="opt">(optional)</span><span v-else class="req">*</span></label>
              <div class="ipt">
                <input
                  type="email"
                  id="adminEmail"
                  v-model="email"
                  :disabled="loading"
                  :readonly="emailLocked"
                  :aria-readonly="emailLocked ? 'true' : undefined"
                  placeholder="you@company.com"
                  autocomplete="email"
                  :required="!claimRequired || linkMode"
                />
              </div>
              <p class="hint">{{ claimRequired && !linkMode
                ? "Leave blank to sign in as admin."
                : "You'll sign in with this email and your password." }}</p>
            </div>

            <!-- Password -->
            <div class="field">
              <div class="labrow">
                <label for="password">Password <span class="req">*</span></label>
                <b v-if="password" class="strength" :style="{ color: strengthColor }">{{ passwordStrengthText }}</b>
              </div>
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

              <div class="meter"><i :style="{ width: `${passwordStrength * 20}%`, background: strengthColor }"></i></div>
                <div class="reqs">
                  <div v-for="req in passwordRequirements" :key="req.label" :class="{ met: req.met }">
                    <svg v-if="req.met" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-width="3" stroke-linecap="round" stroke-linejoin="round" d="M5 13l4 4L19 7"/></svg>
                    <svg v-else fill="none" stroke="currentColor" viewBox="0 0 24 24"><circle cx="12" cy="12" r="9" stroke-width="2"/></svg>
                    {{ req.label }}
                  </div>
                </div>
            </div>

            <!-- Confirm password -->
            <div class="field">
              <div class="labrow">
                <label for="confirmPassword">Confirm password <span class="req">*</span></label>
                <span v-if="password && confirmPassword" class="match" :class="{ bad: !passwordsMatch }">
                <svg v-if="passwordsMatch" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" d="M5 13l4 4L19 7"/></svg>
                <svg v-else fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" d="M6 18L18 6M6 6l12 12"/></svg>
                {{ passwordsMatch ? 'Passwords match' : 'Passwords do not match' }}
                </span>
              </div>
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
              <input id="consentUpdates" type="checkbox" v-model="consentUpdates" :disabled="loading || (claimRequired && !email.trim())" />
              <label for="consentUpdates">Occasionally email me important security &amp; product updates.
                <small>Sends your email{{ company ? ' + company' : '' }} to ability.ai — nothing else. Skippable; disable via env on air-gapped installs.</small>
              </label>
            </div>

            <!-- trinity-enterprise#1329: registration terms, offered by this
                 instance on an emailed setup link only. None offered (404) →
                 nothing here. -->
            <div v-if="termsState === 'unavailable'" class="terms-unavailable" data-testid="registration-terms-unavailable" role="alert">
              <span>The terms could not be loaded.</span>
              <button type="button" class="terms-retry" @click="loadTerms">Retry</button>
            </div>
            <div v-else-if="termsState === 'ready'" class="terms" data-testid="registration-terms">
              <h3 class="terms-title">{{ terms.title || 'Terms' }}</h3>
              <!-- eslint-disable-next-line vue/no-v-html -- sanitized by renderMarkdown (DOMPurify, H-005) -->
              <div v-if="termsHtml" class="terms-text" data-testid="registration-terms-text" tabindex="0" role="region" :aria-label="terms.title || 'Terms'" v-html="termsHtml"></div>
              <a v-if="termsLink" class="terms-link" :href="termsLink" target="_blank" rel="noopener noreferrer">Read the full terms</a>
              <p v-if="termsNotice" class="terms-notice" role="status">{{ termsNotice }}</p>
              <div class="checkrow">
                <input id="acceptRegistrationTerms" type="checkbox" v-model="termsAccepted" :disabled="loading" required />
                <label for="acceptRegistrationTerms">I have read and accept the {{ terms.title || 'terms' }}.</label>
              </div>
            </div>

            <button class="cta" type="submit" :disabled="!isValid || loading">
              <svg v-if="loading" class="spin" fill="none" viewBox="0 0 24 24"><circle class="o25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle><path class="o75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z"></path></svg>
              {{ loading ? 'Creating account…' : 'Create admin account & continue →' }}
            </button>

            <!-- Error: takes the note's one-line slot, so nothing above moves -->
            <div v-if="error" class="secnote errbox">
              <svg fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-width="2" stroke-linecap="round" stroke-linejoin="round" d="M12 8v4m0 4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z"/></svg>
              <span>{{ error }}</span>
            </div>
            <div v-else class="secnote">
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
import { ref, computed, onMounted, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import axios from 'axios'
import { clearSetupCache, getSetupClaimRequired } from '../router'
import { useAuthStore } from '../stores/auth'
import { readSetupLink, LINK_CLAIM_RE, SETUP_LINK_FLAG } from '../utils/setupLink'
import TrinityMark from '../components/TrinityMark.vue'
import { renderMarkdown } from '../utils/markdown'

const router = useRouter()
const route = useRoute()
const authStore = useAuthStore()

// trinity-enterprise#1312: an emailed setup link carries the setup code and the
// admin email. Read once, then cleared from the address bar at once (whatever
// the install), so the code is not left in the URL or the history entry. The
// code is held only here, never stored; a reload is told to open the link again.
const link = readSetupLink(route)
let reopened = false
try {
  if (link.present) sessionStorage.setItem(SETUP_LINK_FLAG, '1')
  else reopened = sessionStorage.getItem(SETUP_LINK_FLAG) === '1'
} catch { /* storage unavailable: a reload then shows the typed form */ }
if (link.present) router.replace({ path: '/setup' })

// #3004: 'instance-id' when this install's first admin must give the EC2
// instance ID, null when not. Fetched by the router guard before this page
// rendered; `undefined` means that fetch failed, so ask again on mount rather
// than render a form that may be missing the ID field.
const claimMode = ref(getSetupClaimRequired())
const claimRequired = computed(() => claimMode.value === 'instance-id')
// Only an instance-id install takes a link; anywhere else its values are ignored.
const linkMode = computed(() => claimRequired.value && (link.present || reopened))
const linkRefused = ref(false)
const linkProblem = computed(() => {
  if (!linkMode.value) return null
  if (linkRefused.value) return 'refused'
  if (!link.present) return 'reopen'
  return LINK_CLAIM_RE.test(link.claim) ? null : 'incomplete'
})
const LINK_PROBLEM_TEXT = {
  refused: "This setup link isn't valid any more. Open the latest link from your email, or ask for a new one.",
  incomplete: 'This setup link is incomplete. Open the latest link from your email, or ask for a new one.',
  reopen: 'To finish setting up, open the link from your email again.',
}
onMounted(async () => {
  if (claimMode.value !== undefined) return
  try {
    const { data } = await axios.get('/api/setup/status')
    claimMode.value = data?.claim_required ?? null
  } catch {
    error.value = 'Could not reach this server to load setup. Check your connection and reload the page.'
  }
})
const EC2_INSTANCES_URL = 'https://console.aws.amazon.com/ec2/home#Instances:'
// Mirrors the backend's one answer for every claim failure (routers/setup.py).
const CLAIM_MISMATCH = 'That instance ID does not match this server.'
const CLAIM_RE = /^i-[0-9a-f]{8,17}$/
const claimCode = ref('')
const claimValid = computed(() => CLAIM_RE.test(claimCode.value.trim().toLowerCase()))

const email = ref('')
// The server refuses any other address for a link, so the one it carried is fixed.
const emailLocked = computed(() => linkMode.value && !!link.email)
watch(emailLocked, (locked) => { if (locked) email.value = link.email }, { immediate: true })
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
    { label: '12+ characters', met: p.length >= 12 },
    { label: 'Uppercase', met: /[A-Z]/.test(p) },
    { label: 'Lowercase', met: /[a-z]/.test(p) },
    { label: 'Number', met: /[0-9]/.test(p) },
    { label: 'Symbol', met: /[^A-Za-z0-9]/.test(p) },
  ]
})

const passwordStrength = computed(() => passwordRequirements.value.filter(r => r.met).length)

// #3884: below all five requirements the meter tops out at "Good" (amber), so it
// never reads as acceptable while submit is disabled. One requirement short, the
// label names it.
const passwordStrengthText = computed(() => {
  const unmet = passwordRequirements.value.filter(r => !r.met)
  if (unmet.length === 1) return `Needs ${unmet[0].label}`
  const texts = ['Very Weak', 'Weak', 'Fair', 'Good', 'Good', 'Excellent']
  return texts[passwordStrength.value] || 'Very Weak'
})

const strengthColor = computed(() => {
  const colors = ['#f87171', '#f87171', '#fb923c', '#fbbf24', '#fbbf24', '#4ade80']
  return colors[passwordStrength.value] || colors[0]
})

// trinity-enterprise#1329: registration terms on an emailed setup link. The
// request carries nothing from the page — never the code, never the email.
// 'none' (404, or no usable answer: the server stays the authority) | 'loading'
// | 'ready' | 'unavailable' (503 or unreachable: Retry, submit disabled).
const TERMS_URL = '/api/setup/registration-terms'
const termsState = ref('none')
const terms = ref(null)
const termsAccepted = ref(false)
const termsNotice = ref(null)
const termsHtml = computed(() => (terms.value?.text ? renderMarkdown(terms.value.text) : ''))
const termsLink = computed(() => {
  const u = terms.value?.url
  return typeof u === 'string' && /^https?:\/\//i.test(u.trim()) ? u.trim() : null
})
const termsOk = computed(() =>
  termsState.value === 'none' || (termsState.value === 'ready' && termsAccepted.value))

async function loadTerms() {
  termsState.value = 'loading'
  termsAccepted.value = false
  try {
    const res = await axios.get(TERMS_URL)
    const data = res?.data
    if (data && typeof data.version === 'string' && data.version) {
      terms.value = data
      termsState.value = 'ready'
    } else {
      terms.value = null
      termsState.value = 'none'
    }
  } catch (e) {
    terms.value = null
    termsState.value = e?.response?.status === 404 ? 'none' : 'unavailable'
  }
}
watch(() => linkMode.value && !linkProblem.value, (open, was) => {
  if (open && !was) loadTerms()
}, { immediate: true })

const isValid = computed(() =>
  claimMode.value !== undefined && termsOk.value &&
  (claimRequired.value && !linkMode.value
    ? claimValid.value && (!email.value.trim() || emailValid.value)
    : emailValid.value) &&
  passwordRequirements.value.every(r => r.met) && passwordsMatch.value
)

async function handleSubmit() {
  if (!isValid.value) return

  loading.value = true
  error.value = null

  const emailValue = email.value.trim()
  try {
    const { data } = await axios.post('/api/setup/admin-password', {
      email: emailValue,
      password: password.value,
      confirm_password: confirmPassword.value,
      company: company.value.trim() || null,
      // The opt-in sends the email, so there is nothing to consent to without one.
      consent_updates: consentUpdates.value && !!emailValue,
      ...(claimRequired.value ? { claim_code: linkMode.value ? link.claim : claimCode.value.trim() } : {}),
      ...(termsState.value === 'ready' ? { registration_terms_version: terms.value.version } : {}),
    })
    try { sessionStorage.removeItem(SETUP_LINK_FLAG) } catch { /* nothing stored */ }

    // Clear the cache so the router knows setup is done.
    clearSetupCache()
    // Sign straight in with what was just chosen (ent#580/#581): the next
    // screen is the first-run overlay, not a second password prompt. By the
    // email just claimed, never a literal 'admin': the account is created under
    // ADMIN_USERNAME (#2381), and the backend accepts the admin's email as the
    // username. Any failure (email not bound, a 2FA challenge, a race) falls
    // back to the login page as before. With no email (allowed only on an
    // instance-ID claim, #3004) the backend returns the admin username instead.
    const identity = emailValue || data?.username
    const signedIn =
      !!identity &&
      (!emailValue || data?.email_registered !== false) &&
      (await authStore.loginWithCredentials(identity, password.value))
    router.push(signedIn ? '/' : '/login')
  } catch (e) {
    const code = e.response?.data?.detail?.code
    if (code === 'registration_terms_changed') {
      await loadTerms()
      termsNotice.value = 'The terms have changed since you opened this page. Please read and accept the current version.'
    } else if (code === 'registration_terms_required') {
      termsAccepted.value = false
      error.value = 'Please accept the terms to continue.'
    } else if (code === 'registration_terms_unavailable') {
      terms.value = null
      termsAccepted.value = false
      termsState.value = 'unavailable'
    } else if (e.response?.status === 403 && e.response?.data?.detail === CLAIM_MISMATCH) {
      // A refused link (replaced, used, or another email) cannot be corrected
      // here: say to use the latest one. A wrong instance ID stays editable.
      if (linkMode.value) linkRefused.value = true
      else error.value = CLAIM_MISMATCH
    } else if (e.response?.status === 403 && linkMode.value) {
      error.value = 'This instance is already set up.'
      router.push({ path: '/login', query: { notice: 'setup-complete' } })
    } else if (e.response?.status === 403) {
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
.hint a{color:var(--indigo-400)}
.meter{height:4px;border-radius:3px;background:var(--g700);margin-top:8px;overflow:hidden}
.meter>i{display:block;height:100%;border-radius:3px;transition:width .3s,background .3s}
.labrow{display:flex;justify-content:space-between;align-items:baseline;gap:8px;margin-bottom:6px}
.labrow label{margin-bottom:0 !important}
.labrow .match{margin-top:0;font-size:11.5px}
.strength{font-size:11.5px;font-weight:600}
.reqs{margin:8px 0 2px;display:flex;flex-wrap:wrap;gap:3px 12px}
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
.secnote.errbox{color:#fca5a5;font-size:12px;text-align:center}
.secnote.errbox svg{color:currentColor}
.cta{width:100%;margin-top:20px;border:0;border-radius:11px;padding:12px;font-size:14px;font-weight:600;color:#fff;cursor:pointer;display:flex;align-items:center;justify-content:center;gap:8px;
  background:linear-gradient(180deg,var(--indigo-500),var(--indigo-600));box-shadow:0 8px 22px -8px rgba(79,70,229,.8);transition:filter .15s}
.cta:hover:not(:disabled){filter:brightness(1.08)}
.cta:disabled{opacity:.5;cursor:not-allowed}
.cta .spin{width:18px;height:18px;animation:rot .8s linear infinite}
.cta .spin .o25{opacity:.25}.cta .spin .o75{opacity:.75}
@keyframes rot{to{transform:rotate(360deg)}}
.linkproblem{display:flex;gap:10px;align-items:flex-start;padding:4px 0 8px;font-size:13px;line-height:1.5;color:var(--ink)}
.linkproblem svg{width:18px;height:18px;color:var(--indigo-400);flex:none;margin-top:1px}
.secnote{display:flex;gap:8px;align-items:center;justify-content:center;margin-top:12px;font-size:11px;color:var(--muted2)}
.secnote svg{width:13px;height:13px;color:var(--indigo-400);flex:none}
.terms{margin-top:14px;padding-top:12px;border-top:1px solid var(--g700)}
.terms-title{margin:0 0 8px;font-size:13px;font-weight:600;color:var(--ink)}
.terms-text{max-height:180px;overflow-y:auto;overscroll-behavior:contain;background:var(--g900);border:1px solid var(--g700);border-radius:10px;padding:10px 12px;font-size:12px;line-height:1.5;color:var(--muted);overflow-wrap:anywhere}
.terms-text:focus-visible,.terms-retry:focus-visible,.terms-link:focus-visible{outline:2px solid var(--indigo-500);outline-offset:2px}
.terms-text :deep(h1),.terms-text :deep(h2),.terms-text :deep(h3){font-size:12.5px;margin:8px 0 4px;color:var(--ink)}
.terms-text :deep(p),.terms-text :deep(ul),.terms-text :deep(ol){margin:0 0 6px}
.terms-text :deep(a),.terms-link{color:var(--indigo-400)}
.terms-link{display:inline-block;font-size:12px;margin:8px 0 2px}
.terms-notice{font-size:12px;color:var(--ink);margin:8px 0 0}
.terms .checkrow{margin-top:10px}
.terms-unavailable{display:flex;gap:10px;align-items:center;justify-content:space-between;margin-top:14px;font-size:12.5px;color:var(--ink);flex-wrap:wrap}
.terms-retry{background:transparent;border:1px solid var(--g700);border-radius:8px;padding:5px 12px;font-size:12px;color:var(--ink);cursor:pointer}
.terms-retry:hover{border-color:var(--indigo-500)}
.footnote{text-align:center;font-size:11.5px;color:var(--muted2);margin-top:10px;line-height:1.5}
.footnote code{background:var(--g800);padding:1px 5px;border-radius:4px;color:#cbd2e3;font-size:11px}

/* Respect reduced-motion: freeze the constellation into a composed static state. */
@media (prefers-reduced-motion: reduce){
  .aurora::before,.aurora::after,.o1,.o2,.o3,.node,.core,.halo,.cta .spin{animation:none}
  .halo{opacity:0}
}
</style>
