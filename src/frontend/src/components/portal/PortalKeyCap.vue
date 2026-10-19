<template>
  <!-- `<kbd>` on gray + `font-mono`, the Dashboard's shape: a key cap reads as
       a key cap without a hue claiming meaning it has not got. ONE recipe for
       the three places a chord is printed (the key list, the rail's tips, the
       sidebar search field), so they cannot drift apart. -->
  <kbd class="shrink-0 whitespace-nowrap rounded border border-gray-200 bg-gray-50 px-1.5 py-0.5 text-[11px] font-mono text-gray-600 dark:border-gray-700 dark:bg-gray-900 dark:text-gray-300"><slot /></kbd>
</template>
