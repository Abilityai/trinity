"""
Agent resource limits, parallel capacity, and execution timeout operations.

Handles memory/CPU limits, max parallel tasks, and task timeout settings.
"""

from typing import Optional, Dict

from db.connection import get_db_connection


class ResourcesMixin:
    """Mixin for agent resource limits, parallel capacity, and execution timeout."""

    # =========================================================================
    # Resource Limits
    # =========================================================================

    def get_resource_limits(self, agent_name: str) -> Optional[Dict[str, str]]:
        """
        Get per-agent resource limits (memory and CPU).

        Returns None if no custom limits are set, otherwise returns dict with:
        - memory: Memory limit (e.g., "8g", "16g")
        - cpu: CPU limit (e.g., "4", "8")
        """
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT memory_limit, cpu_limit
                FROM agent_ownership WHERE agent_name = ? AND deleted_at IS NULL
            """, (agent_name,))
            row = cursor.fetchone()
            if row:
                memory = row["memory_limit"]
                cpu = row["cpu_limit"]
                # Return None if no custom limits set
                if memory is None and cpu is None:
                    return None
                return {
                    "memory": memory,
                    "cpu": cpu
                }
            return None

    def set_resource_limits(self, agent_name: str, memory: Optional[str] = None, cpu: Optional[str] = None) -> bool:
        """
        Set per-agent resource limits.

        Args:
            agent_name: Name of the agent
            memory: Memory limit (e.g., "4g", "8g", "16g") or None to clear
            cpu: CPU limit (e.g., "2", "4", "8") or None to clear

        Returns:
            True if update succeeded, False otherwise
        """
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE agent_ownership SET memory_limit = ?, cpu_limit = ?
                WHERE agent_name = ?
            """, (memory, cpu, agent_name))
            conn.commit()
            return cursor.rowcount > 0

    # =========================================================================
    # Parallel Capacity (CAPACITY-001)
    # =========================================================================

    def get_max_parallel_tasks(self, agent_name: str) -> int:
        """
        Get max_parallel_tasks for an agent (default: 3).

        Args:
            agent_name: Name of the agent

        Returns:
            Maximum number of parallel tasks allowed (1-10, default 3)
        """
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT COALESCE(max_parallel_tasks, 3) as max_parallel_tasks
                FROM agent_ownership WHERE agent_name = ? AND deleted_at IS NULL
            """, (agent_name,))
            row = cursor.fetchone()
            if row:
                return row["max_parallel_tasks"]
            return 3  # Default

    def set_max_parallel_tasks(self, agent_name: str, max_tasks: int) -> bool:
        """
        Set max_parallel_tasks for an agent.

        Args:
            agent_name: Name of the agent
            max_tasks: Maximum parallel tasks (must be 1-10)

        Returns:
            True if update succeeded
        """
        # Validate range
        if max_tasks < 1 or max_tasks > 10:
            return False

        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE agent_ownership SET max_parallel_tasks = ?
                WHERE agent_name = ?
            """, (max_tasks, agent_name))
            conn.commit()
            return cursor.rowcount > 0

    def get_all_agents_parallel_capacity(self) -> Dict[str, int]:
        """
        Get max_parallel_tasks for all agents.

        Returns:
            Dict mapping agent_name to max_parallel_tasks
        """
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT agent_name, COALESCE(max_parallel_tasks, 3) as max_parallel_tasks
                FROM agent_ownership
                WHERE deleted_at IS NULL
            """)
            return {row["agent_name"]: row["max_parallel_tasks"] for row in cursor.fetchall()}

    # =========================================================================
    # Dispatch Circuit Breaker opt-in (RELIABILITY-007, #526)
    # =========================================================================

    def get_circuit_breaker_enabled(self, agent_name: str) -> bool:
        """Per-agent dispatch-breaker opt-in flag (default False — opt-in canary).

        Read on the dispatch path alongside ``get_max_parallel_tasks``; the
        caller gates again on the global ``DISPATCH_BREAKER_ENABLED`` master
        switch so both must be on for the breaker to engage (D7/D11).
        """
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT COALESCE(circuit_breaker_enabled, 0) as circuit_breaker_enabled
                FROM agent_ownership WHERE agent_name = ? AND deleted_at IS NULL
            """, (agent_name,))
            row = cursor.fetchone()
            if row:
                return bool(row["circuit_breaker_enabled"])
            return False

    def set_circuit_breaker_enabled(self, agent_name: str, enabled: bool) -> bool:
        """Enable/disable the per-agent dispatch breaker.

        Returns:
            True if the row was updated.
        """
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE agent_ownership SET circuit_breaker_enabled = ?
                WHERE agent_name = ? AND deleted_at IS NULL
            """, (1 if enabled else 0, agent_name))
            conn.commit()
            return cursor.rowcount > 0

    def get_all_circuit_breaker_enabled(self) -> Dict[str, bool]:
        """Bulk opt-in flags for all live agents.

        Powers the slots-dashboard circuit badge without an N+1 SELECT.
        """
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT agent_name, COALESCE(circuit_breaker_enabled, 0) as cbe
                FROM agent_ownership
                WHERE deleted_at IS NULL
            """)
            return {row["agent_name"]: bool(row["cbe"]) for row in cursor.fetchall()}

    # =========================================================================
    # Execution Timeout (TIMEOUT-001)
    # =========================================================================

    def get_execution_timeout(self, agent_name: str) -> int:
        """
        Get execution_timeout_seconds for an agent (default: 3600 = 60 minutes).

        Args:
            agent_name: Name of the agent

        Returns:
            Timeout in seconds (default 3600)
        """
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT COALESCE(execution_timeout_seconds, 3600) as execution_timeout_seconds
                FROM agent_ownership WHERE agent_name = ? AND deleted_at IS NULL
            """, (agent_name,))
            row = cursor.fetchone()
            if row:
                return row["execution_timeout_seconds"]
            return 3600  # Default 60 minutes (#665)

    def get_all_execution_timeouts(self) -> Dict[str, int]:
        """
        Get execution_timeout_seconds for all agents.

        Returns:
            Dict mapping agent_name to timeout in seconds.
        """
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT agent_name, COALESCE(execution_timeout_seconds, 3600) as timeout
                FROM agent_ownership
                WHERE deleted_at IS NULL
            """)
            return {row["agent_name"]: row["timeout"] for row in cursor.fetchall()}

    def set_execution_timeout(self, agent_name: str, timeout_seconds: int) -> bool:
        """
        Set execution_timeout_seconds for an agent.

        Args:
            agent_name: Name of the agent
            timeout_seconds: Timeout in seconds (must be 60-7200, i.e., 1 min to 2 hours)

        Returns:
            True if update succeeded
        """
        # Validate range: 1 minute minimum, 2 hours maximum
        if timeout_seconds < 60 or timeout_seconds > 7200:
            return False

        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE agent_ownership SET execution_timeout_seconds = ?
                WHERE agent_name = ?
            """, (timeout_seconds, agent_name))
            conn.commit()
            return cursor.rowcount > 0

    # =========================================================================
    # Backlog Depth (BACKLOG-001)
    # =========================================================================

    def get_max_backlog_depth(self, agent_name: str) -> int:
        """
        Get max_backlog_depth for an agent (default: 50, cap: 200).

        The backlog holds async tasks that arrived while all parallel slots were
        busy. When a slot frees, the oldest queued item drains automatically.
        """
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT COALESCE(max_backlog_depth, 50) as max_backlog_depth
                FROM agent_ownership WHERE agent_name = ? AND deleted_at IS NULL
                """,
                (agent_name,),
            )
            row = cursor.fetchone()
            if row:
                return row["max_backlog_depth"]
            return 50

    def set_max_backlog_depth(self, agent_name: str, depth: int) -> bool:
        """
        Set max_backlog_depth for an agent (valid range 1-200).

        Returns False on out-of-range input or if the agent doesn't exist.
        """
        if depth < 1 or depth > 200:
            return False
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                UPDATE agent_ownership SET max_backlog_depth = ?
                WHERE agent_name = ?
                """,
                (depth, agent_name),
            )
            conn.commit()
            return cursor.rowcount > 0
