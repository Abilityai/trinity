import { computed } from 'vue'
import { useAuthStore } from '../stores/auth'

/**
 * Composable for ROLE-001 hierarchy checks (#302).
 *
 * Backend's role model (4-tier): user < operator < creator < admin.
 * This composable mirrors `dependencies.py:require_role()` for client-side
 * UI gating. The backend is still the security boundary — UI hiding is
 * convenience, not enforcement.
 *
 * Usage:
 *   const { isAdmin, hasMinRole } = useRole()
 *   if (isAdmin.value) { ... }
 *   if (hasMinRole.value('creator')) { ... }
 */

const ROLE_HIERARCHY = ['user', 'operator', 'creator', 'admin']

export function useRole() {
  const auth = useAuthStore()

  const role = computed(() => auth.role)
  const roleIndex = computed(() => ROLE_HIERARCHY.indexOf(role.value))

  const isAdmin = computed(() => role.value === 'admin')

  function hasMinRole(minRole) {
    const minIndex = ROLE_HIERARCHY.indexOf(minRole)
    if (minIndex === -1) return false  // unknown role name
    return roleIndex.value >= minIndex
  }

  return { role, roleIndex, isAdmin, hasMinRole }
}
