import { computed, ref, toValue, watch } from 'vue'
import { categoryOptions, filterSkills, sourceOptions } from '../utils/skillFilter'

/**
 * The state of `components/skills/SkillFilterBar.vue` over one list of skills
 * (trinity-enterprise#660): the Assign skills dialog and the Library.
 *
 * A choice the list no longer offers clears itself. A sync can drop a source,
 * the bar then hides its select, and a value nobody can see or change would
 * keep narrowing the list.
 *
 * @param {import('vue').MaybeRefOrGetter<Array>} skills
 */
export function useSkillFilter(skills) {
  const query = ref('')
  const source = ref('')
  const category = ref('')

  const list = computed(() => toValue(skills) || [])
  const sources = computed(() => sourceOptions(list.value))
  const categories = computed(() => categoryOptions(list.value))

  watch(sources, (options) => {
    if (options.length < 2 || !options.some((o) => o.value === source.value)) source.value = ''
  })
  watch(categories, (options) => {
    if (!options.some((o) => o.value === category.value)) category.value = ''
  })

  const filtered = computed(() => filterSkills(list.value, {
    query: query.value, source: source.value, category: category.value,
  }))
  const active = computed(() => Boolean(query.value.trim() || source.value || category.value))

  function reset() {
    query.value = ''
    source.value = ''
    category.value = ''
  }

  return { query, source, category, sourceOptions: sources, categoryOptions: categories, filtered, active, reset }
}
