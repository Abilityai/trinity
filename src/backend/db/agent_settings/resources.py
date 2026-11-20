"""
Agent resource limits, parallel capacity, and execution timeout operations.

Handles memory/CPU limits, max parallel tasks, and task timeout settings.

Converted from raw sqlite3 to SQLAlchemy Core (#300) so it runs unchanged on
both SQLite and PostgreSQL. Queries are built from the ``agent_ownership`` table
handle in ``db/tables.py``; the engine is resolved via ``db/engine.py``. Method
signatures, return shapes, and behavior are unchanged.
"""

from typing import Optional, Dict

from sqlalchemy import select, update, func

from ..engine import get_engine
from ..tables import agent_ownership


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
        stmt = select(
            agent_ownership.c.memory_limit,
            agent_ownership.c.cpu_limit,
        ).where(
            (agent_ownership.c.agent_name == agent_name)
            & (agent_ownership.c.deleted_at.is_(None))
        )
        with get_engine().connect() as conn:
            row = conn.execute(stmt).mappings().first()
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
        stmt = (
            update(agent_ownership)
            .where(agent_ownership.c.agent_name == agent_name)
            .values(memory_limit=memory, cpu_limit=cpu)
        )
        with get_engine().begin() as conn:
            result = conn.execute(stmt)
            return result.rowcount > 0

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
        stmt = select(
            func.coalesce(agent_ownership.c.max_parallel_tasks, 3).label("max_parallel_tasks")
        ).where(
            (agent_ownership.c.agent_name == agent_name)
            & (agent_ownership.c.deleted_at.is_(None))
        )
        with get_engine().connect() as conn:
            row = conn.execute(stmt).mappings().first()
        if row:
            return row["max_parallel_tasks"]
        return 3  # Default

    def set_max_parallel_tasks(self, agent_name: str, max_tasks: int) -> bool:
        """
        Set max_parallel_tasks for an agent.

        Args:
            agent_name: Name of the agent
            max_tasks: Maximum parallel tasks (must be >= 1; the upper bound is
                the admin-set fleet ceiling, enforced by the router)

        Returns:
            True if update succeeded
        """
        # Lower bound only: the upper bound is the fleet ceiling (#506),
        # validated by the router, not a hardcoded 10 (#4564).
        if max_tasks < 1:
            return False

        stmt = (
            update(agent_ownership)
            .where(agent_ownership.c.agent_name == agent_name)
            .values(max_parallel_tasks=max_tasks)
        )
        with get_engine().begin() as conn:
            result = conn.execute(stmt)
            return result.rowcount > 0

    def get_all_agents_parallel_capacity(self) -> Dict[str, int]:
        """
        Get max_parallel_tasks for all agents.

        Returns:
            Dict mapping agent_name to max_parallel_tasks
        """
        stmt = select(
            agent_ownership.c.agent_name,
            func.coalesce(agent_ownership.c.max_parallel_tasks, 3).label("max_parallel_tasks"),
        ).where(agent_ownership.c.deleted_at.is_(None))
        with get_engine().connect() as conn:
            return {
                row["agent_name"]: row["max_parallel_tasks"]
                for row in conn.execute(stmt).mappings()
            }

    # =========================================================================
    # Dispatch Circuit Breaker opt-in (RELIABILITY-007, #526)
    # =========================================================================

    def get_circuit_breaker_enabled(self, agent_name: str) -> bool:
        """Per-agent dispatch-breaker opt-in flag (default False — opt-in canary).

        Read on the dispatch path alongside ``get_max_parallel_tasks``; the
        caller gates again on the global ``DISPATCH_BREAKER_ENABLED`` master
        switch so both must be on for the breaker to engage (D7/D11).
        """
        stmt = select(
            func.coalesce(agent_ownership.c.circuit_breaker_enabled, 0).label("circuit_breaker_enabled")
        ).where(
            (agent_ownership.c.agent_name == agent_name)
            & (agent_ownership.c.deleted_at.is_(None))
        )
        with get_engine().connect() as conn:
            row = conn.execute(stmt).mappings().first()
        if row:
            return bool(row["circuit_breaker_enabled"])
        return False

    def set_circuit_breaker_enabled(self, agent_name: str, enabled: bool) -> bool:
        """Enable/disable the per-agent dispatch breaker.

        Returns:
            True if the row was updated.
        """
        stmt = (
            update(agent_ownership)
            .where(
                (agent_ownership.c.agent_name == agent_name)
                & (agent_ownership.c.deleted_at.is_(None))
            )
            .values(circuit_breaker_enabled=1 if enabled else 0)
        )
        with get_engine().begin() as conn:
            result = conn.execute(stmt)
            return result.rowcount > 0

    def get_all_circuit_breaker_enabled(self) -> Dict[str, bool]:
        """Bulk opt-in flags for all live agents.

        Powers the slots-dashboard circuit badge without an N+1 SELECT.
        """
        stmt = select(
            agent_ownership.c.agent_name,
            func.coalesce(agent_ownership.c.circuit_breaker_enabled, 0).label("cbe"),
        ).where(agent_ownership.c.deleted_at.is_(None))
        with get_engine().connect() as conn:
            return {
                row["agent_name"]: bool(row["cbe"])
                for row in conn.execute(stmt).mappings()
            }

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
        stmt = select(
            func.coalesce(agent_ownership.c.execution_timeout_seconds, 3600).label("execution_timeout_seconds")
        ).where(
            (agent_ownership.c.agent_name == agent_name)
            & (agent_ownership.c.deleted_at.is_(None))
        )
        with get_engine().connect() as conn:
            row = conn.execute(stmt).mappings().first()
        if row:
            return row["execution_timeout_seconds"]
        return 3600  # Default 60 minutes (#665)

    def get_all_execution_timeouts(self) -> Dict[str, int]:
        """
        Get execution_timeout_seconds for all agents.

        Returns:
            Dict mapping agent_name to timeout in seconds.
        """
        stmt = select(
            agent_ownership.c.agent_name,
            func.coalesce(agent_ownership.c.execution_timeout_seconds, 3600).label("timeout"),
        ).where(agent_ownership.c.deleted_at.is_(None))
        with get_engine().connect() as conn:
            return {
                row["agent_name"]: row["timeout"]
                for row in conn.execute(stmt).mappings()
            }

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

        stmt = (
            update(agent_ownership)
            .where(agent_ownership.c.agent_name == agent_name)
            .values(execution_timeout_seconds=timeout_seconds)
        )
        with get_engine().begin() as conn:
            result = conn.execute(stmt)
            return result.rowcount > 0

    # =========================================================================
    # Backlog Depth (BACKLOG-001)
    # =========================================================================

    def get_max_backlog_depth(self, agent_name: str) -> int:
        """
        Get max_backlog_depth for an agent (default: 50, cap: 200).

        The backlog holds async tasks that arrived while all parallel slots were
        busy. When a slot frees, the oldest queued item drains automatically.
        """
        stmt = select(
            func.coalesce(agent_ownership.c.max_backlog_depth, 50).label("max_backlog_depth")
        ).where(
            (agent_ownership.c.agent_name == agent_name)
            & (agent_ownership.c.deleted_at.is_(None))
        )
        with get_engine().connect() as conn:
            row = conn.execute(stmt).mappings().first()
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
        stmt = (
            update(agent_ownership)
            .where(agent_ownership.c.agent_name == agent_name)
            .values(max_backlog_depth=depth)
        )
        with get_engine().begin() as conn:
            result = conn.execute(stmt)
            return result.rowcount > 0
