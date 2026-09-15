<!--
  First-run step schematics (ent#581 spec comment 3): the technical-schematic
  family, hand-drawn inline SVG on `currentColor` — themes for free, no image
  files, nothing to load and so nothing to reserve space for. One vocabulary at
  one stroke weight: rounded rectangle = hardware you own, filled circle = live
  / connected, open circle = exists but not configured, dashed line = a path
  that carries nothing yet, tick marks = a measured run.

  Informative, not decorative: each carries a real `aria-label`, so
  `aria-hidden` would be wrong. Sources are verbatim from the spec.
-->
<template>
  <svg
    viewBox="0 0 200 140"
    role="img"
    :aria-label="LABELS[name]"
    fill="none"
    class="h-[140px] w-[200px] flex-none text-action-primary-600 dark:text-action-primary-500 opacity-90 max-sm:h-[118px] max-sm:w-[168px]"
  >
    <template v-if="name === 'secure'">
      <rect x="16" y="52" width="42" height="36" rx="4" fill="none" stroke="currentColor" stroke-width="1.4"/>
      <path d="M24 62 H44 M24 70 H44 M24 78 H36" stroke="currentColor" stroke-width="1.2" opacity=".45"/>
      <circle cx="66" cy="70" r="3.5" fill="none" stroke="currentColor" stroke-width="1.4"/>
      <path d="M70 70 H84" stroke="currentColor" stroke-width="1.4"/>
      <g stroke="currentColor" stroke-width="1" opacity=".4">
        <path d="M73 66 V74 M77 66 V74 M81 66 V74"/>
      </g>
      <path d="M100 44 L118 51 V70 C118 82 100 92 100 92 C100 92 82 82 82 70 V51 Z" fill="none" stroke="currentColor" stroke-width="1.4"/>
      <path d="M93 68 L98 73 L108 62" fill="none" stroke="currentColor" stroke-width="1.4" opacity=".55"/>
      <path d="M122 70 H136" stroke="currentColor" stroke-width="1.4"/>
      <g stroke="currentColor" stroke-width="1" opacity=".4">
        <path d="M125 66 V74 M129 66 V74 M133 66 V74"/>
      </g>
      <circle cx="140" cy="70" r="3.5" fill="none" stroke="currentColor" stroke-width="1.4"/>
      <path d="M156 78 A 11 11 0 0 1 158 56 A 14 14 0 0 1 182 60 A 9 9 0 0 1 184 78 Z" fill="none" stroke="currentColor" stroke-width="1.4"/>
      <path d="M58 104 H140" stroke="currentColor" stroke-width="1.2" opacity=".3" stroke-dasharray="4 5"/>
      <path d="M95 100 L103 108 M103 100 L95 108" stroke="currentColor" stroke-width="1.4" opacity=".45"/>
    </template>

    <template v-else-if="name === 'email'">
      <circle cx="58" cy="70" r="20" fill="none" stroke="currentColor" stroke-width="1.4"/>
      <circle cx="58" cy="63" r="7" fill="none" stroke="currentColor" stroke-width="1.4" opacity=".55"/>
      <path d="M45 84 A 15 15 0 0 1 71 84" fill="none" stroke="currentColor" stroke-width="1.4" opacity=".55"/>
      <path d="M82 70 H104" stroke="currentColor" stroke-width="1.4" stroke-dasharray="4 5" opacity=".6"/>
      <rect x="112" y="52" width="56" height="38" rx="4" fill="none" stroke="currentColor" stroke-width="1.4"/>
      <path d="M112 56 L140 74 L168 56" fill="none" stroke="currentColor" stroke-width="1.4" opacity=".55"/>
      <g fill="currentColor" opacity=".45">
        <circle cx="124" cy="104" r="2.5"/><circle cx="134" cy="104" r="2.5"/><circle cx="144" cy="104" r="2.5"/>
        <circle cx="154" cy="104" r="2.5"/><circle cx="164" cy="104" r="2.5"/><circle cx="174" cy="104" r="2.5"/>
      </g>
    </template>

    <template v-else-if="name === 'claude'">
      <path d="M30 40 H150" stroke="currentColor" stroke-width="1.4"/>
      <circle cx="164" cy="40" r="10" fill="none" stroke="currentColor" stroke-width="1.4"/>
      <path d="M154 40 H136" stroke="currentColor" stroke-width="1.4"/>
      <path d="M142 40 V48 M132 40 V46" stroke="currentColor" stroke-width="1.4"/>
      <circle cx="164" cy="40" r="3.5" fill="currentColor"/>
      <g stroke="currentColor" stroke-width="1.4" opacity=".5">
        <path d="M52 40 V78"/><path d="M92 40 V78"/><path d="M132 40 V78"/>
      </g>
      <circle cx="52" cy="90" r="12" fill="none" stroke="currentColor" stroke-width="1.4"/>
      <circle cx="92" cy="90" r="12" fill="none" stroke="currentColor" stroke-width="1.4"/>
      <circle cx="132" cy="90" r="12" fill="none" stroke="currentColor" stroke-width="1.4"/>
      <circle cx="52" cy="90" r="4" fill="currentColor"/>
      <circle cx="92" cy="90" r="4" fill="currentColor"/>
      <circle cx="132" cy="90" r="4" fill="currentColor"/>
    </template>

    <template v-else-if="name === 'keys'">
      <rect x="20" y="40" width="46" height="60" rx="5" fill="none" stroke="currentColor" stroke-width="1.4"/>
      <path d="M28 52 H58 M28 60 H58" stroke="currentColor" stroke-width="1.2" opacity=".4"/>
      <path d="M66 54 H108" stroke="currentColor" stroke-width="1.4"/>
      <rect x="112" y="46" width="22" height="16" rx="2" fill="currentColor" opacity=".85"/>
      <path d="M66 70 H108" stroke="currentColor" stroke-width="1.4" stroke-dasharray="4 5" opacity=".5"/>
      <rect x="112" y="62" width="22" height="16" rx="2" fill="none" stroke="currentColor" stroke-width="1.4" opacity=".5"/>
      <path d="M66 86 H108" stroke="currentColor" stroke-width="1.4" stroke-dasharray="4 5" opacity=".5"/>
      <rect x="112" y="78" width="22" height="16" rx="2" fill="none" stroke="currentColor" stroke-width="1.4" opacity=".5"/>
      <path d="M142 54 H176" stroke="currentColor" stroke-width="1.4" opacity=".7"/>
      <path d="M142 70 H162" stroke="currentColor" stroke-width="1.2" opacity=".25"/>
      <path d="M142 86 H162" stroke="currentColor" stroke-width="1.2" opacity=".25"/>
    </template>

    <template v-else-if="name === 'agent'">
      <path d="M100 46 L134 104 L66 104 Z" fill="none" stroke="currentColor" stroke-width="1.4" opacity=".45"/>
      <circle cx="100" cy="46" r="13" fill="none" stroke="currentColor" stroke-width="1.4"/>
      <circle cx="100" cy="46" r="5" fill="currentColor"/>
      <circle cx="134" cy="104" r="9" fill="none" stroke="currentColor" stroke-width="1.4" opacity=".6"/>
      <circle cx="134" cy="104" r="3.5" fill="currentColor" opacity=".6"/>
      <circle cx="66" cy="104" r="9" fill="none" stroke="currentColor" stroke-width="1.4" opacity=".6"/>
      <circle cx="66" cy="104" r="3.5" fill="currentColor" opacity=".6"/>
      <path d="M126 24 H176 A4 4 0 0 1 180 28 V48 A4 4 0 0 1 176 52 H140 L132 60 V52 H126 A4 4 0 0 1 122 48 V28 A4 4 0 0 1 126 24 Z" fill="none" stroke="currentColor" stroke-width="1.4"/>
      <path d="M132 34 H166 M132 42 H154" stroke="currentColor" stroke-width="1.2" opacity=".45"/>
    </template>

    <template v-else-if="name === 'sharing'">
      <rect x="16" y="34" width="104" height="72" rx="8" fill="none" stroke="currentColor" stroke-width="1.4" opacity=".45"/>
      <g fill="currentColor" opacity=".55">
        <circle cx="40" cy="56" r="3"/><circle cx="58" cy="70" r="3"/><circle cx="40" cy="84" r="3"/>
        <circle cx="76" cy="56" r="3"/><circle cx="94" cy="70" r="3"/><circle cx="76" cy="84" r="3"/>
      </g>
      <path d="M120 58 L140 68 V72 L120 82" fill="none" stroke="currentColor" stroke-width="1.4"/>
      <path d="M144 70 H176" stroke="currentColor" stroke-width="1.4" stroke-dasharray="4 5" opacity=".6"/>
      <g fill="currentColor" opacity=".5">
        <circle cx="152" cy="70" r="2.5"/><circle cx="164" cy="70" r="2.5"/>
      </g>
      <circle cx="182" cy="70" r="5" fill="none" stroke="currentColor" stroke-width="1.4" opacity=".5"/>
    </template>
  </svg>
</template>

<script setup>
defineProps({
  name: {
    type: String,
    required: true,
    validator: (v) => ['secure', 'email', 'claude', 'keys', 'agent', 'sharing'].includes(v),
  },
})

const LABELS = {
  secure: 'Diagram: this server reaches the public network through a named, gated route.',
  email: 'Diagram: an identity bound to the admin account, with a one-time code in transit.',
  claude: 'Diagram: one credential feeds every agent on the instance.',
  keys: 'Diagram: three optional ports on the instance, one connected and two open.',
  agent: 'Diagram: three running agents, one of them in conversation.',
  sharing: 'Diagram: coarse counts leaving the instance through a narrowing aperture.',
}
</script>
