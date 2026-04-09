"""
Historical storage for intercept records.

Provides the central master store for intercept history. Remote agents
keep short-lived local buffers; the control plane owns the authoritative
historical record.

Records include:
    - Intercept metadata (agent, mode, timestamps, location)
    - Links to derived outputs (decoded messages, telemetry)
    - Links to raw artifacts (I/Q captures, waterfalls) when retained
    - Retention policy metadata
    - Task/job tracking records
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from enum import Enum

logger = logging.getLogger('intercept.history_store')


class TaskState(str, Enum):
    """State of a task/job."""
    PENDING = 'pending'
    ASSIGNED = 'assigned'
    RUNNING = 'running'
    COMPLETED = 'completed'
    FAILED = 'failed'
    CANCELLED = 'cancelled'


# =============================================================================
# Database Schema (applied via init_history_tables)
# =============================================================================

HISTORY_SCHEMA = '''
    CREATE TABLE IF NOT EXISTS intercept_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        agent_id INTEGER,
        agent_name TEXT,
        mode TEXT NOT NULL,
        started_at TIMESTAMP,
        ended_at TIMESTAMP,
        latitude REAL,
        longitude REAL,
        location_label TEXT,
        summary TEXT,
        decoded_output TEXT,
        raw_artifact_path TEXT,
        retention_policy TEXT DEFAULT 'standard',
        metadata TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (agent_id) REFERENCES agents(id)
    );

    CREATE INDEX IF NOT EXISTS idx_history_mode ON intercept_history(mode);
    CREATE INDEX IF NOT EXISTS idx_history_agent ON intercept_history(agent_id);
    CREATE INDEX IF NOT EXISTS idx_history_started ON intercept_history(started_at);

    CREATE TABLE IF NOT EXISTS tasks (
        id TEXT PRIMARY KEY,
        mode TEXT NOT NULL,
        agent_id INTEGER,
        agent_name TEXT,
        lease_id TEXT,
        state TEXT NOT NULL DEFAULT 'pending',
        params TEXT,
        result_summary TEXT,
        artifact_ids TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        assigned_at TIMESTAMP,
        started_at TIMESTAMP,
        completed_at TIMESTAMP,
        error_message TEXT,
        FOREIGN KEY (agent_id) REFERENCES agents(id)
    );

    CREATE INDEX IF NOT EXISTS idx_tasks_state ON tasks(state);
    CREATE INDEX IF NOT EXISTS idx_tasks_agent ON tasks(agent_id);

    CREATE TABLE IF NOT EXISTS agent_buffers (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        agent_id INTEGER NOT NULL,
        agent_name TEXT,
        scan_type TEXT NOT NULL,
        payload TEXT NOT NULL,
        buffered_at TIMESTAMP NOT NULL,
        synced_at TIMESTAMP,
        retry_count INTEGER DEFAULT 0,
        FOREIGN KEY (agent_id) REFERENCES agents(id)
    );

    CREATE INDEX IF NOT EXISTS idx_buffers_agent ON agent_buffers(agent_id);
    CREATE INDEX IF NOT EXISTS idx_buffers_synced ON agent_buffers(synced_at);
'''


def init_history_tables() -> None:
    """Create history tables if they don't exist."""
    try:
        from utils.database import get_db
        with get_db() as conn:
            conn.executescript(HISTORY_SCHEMA)
        logger.info("History tables initialized")
    except Exception:
        logger.exception("Failed to initialize history tables")


# =============================================================================
# Intercept History CRUD
# =============================================================================

def store_intercept(
    mode: str,
    agent_id: int | None = None,
    agent_name: str | None = None,
    started_at: str | None = None,
    ended_at: str | None = None,
    latitude: float | None = None,
    longitude: float | None = None,
    location_label: str | None = None,
    summary: str | None = None,
    decoded_output: dict | list | None = None,
    raw_artifact_path: str | None = None,
    retention_policy: str = 'standard',
    metadata: dict | None = None,
) -> int:
    """
    Store an intercept record in central history.

    Returns:
        Record ID
    """
    from utils.database import get_db
    with get_db() as conn:
        cursor = conn.execute('''
            INSERT INTO intercept_history
            (agent_id, agent_name, mode, started_at, ended_at,
             latitude, longitude, location_label, summary,
             decoded_output, raw_artifact_path, retention_policy, metadata)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            agent_id, agent_name, mode, started_at, ended_at,
            latitude, longitude, location_label, summary,
            json.dumps(decoded_output) if decoded_output else None,
            raw_artifact_path, retention_policy,
            json.dumps(metadata) if metadata else None,
        ))
        return cursor.lastrowid


def get_intercept(record_id: int) -> dict | None:
    """Get an intercept record by ID."""
    from utils.database import get_db
    with get_db() as conn:
        cursor = conn.execute(
            'SELECT * FROM intercept_history WHERE id = ?', (record_id,)
        )
        row = cursor.fetchone()
        return _row_to_intercept(row) if row else None


def search_intercepts(
    mode: str | None = None,
    agent_id: int | None = None,
    since: str | None = None,
    until: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[dict]:
    """
    Search intercept history with filters.

    Args:
        mode: Filter by mode name
        agent_id: Filter by agent
        since: ISO timestamp, records after this time
        until: ISO timestamp, records before this time
        limit: Max results
        offset: Pagination offset

    Returns:
        List of intercept record dicts
    """
    from utils.database import get_db
    conditions = []
    params: list = []

    if mode:
        conditions.append('mode = ?')
        params.append(mode)
    if agent_id is not None:
        conditions.append('agent_id = ?')
        params.append(agent_id)
    if since:
        conditions.append('started_at >= ?')
        params.append(since)
    if until:
        conditions.append('started_at <= ?')
        params.append(until)

    where = f'WHERE {" AND ".join(conditions)}' if conditions else ''
    params.extend([limit, offset])

    with get_db() as conn:
        cursor = conn.execute(f'''
            SELECT * FROM intercept_history
            {where}
            ORDER BY created_at DESC
            LIMIT ? OFFSET ?
        ''', params)
        return [_row_to_intercept(row) for row in cursor]


def count_intercepts(
    mode: str | None = None,
    agent_id: int | None = None,
) -> int:
    """Count intercept records with optional filters."""
    from utils.database import get_db
    conditions = []
    params: list = []

    if mode:
        conditions.append('mode = ?')
        params.append(mode)
    if agent_id is not None:
        conditions.append('agent_id = ?')
        params.append(agent_id)

    where = f'WHERE {" AND ".join(conditions)}' if conditions else ''

    with get_db() as conn:
        cursor = conn.execute(
            f'SELECT COUNT(*) FROM intercept_history {where}', params
        )
        return cursor.fetchone()[0]


def cleanup_old_intercepts(max_age_days: int = 90) -> int:
    """Remove intercept records older than max_age_days."""
    from utils.database import get_db
    with get_db() as conn:
        cursor = conn.execute('''
            DELETE FROM intercept_history
            WHERE created_at < datetime('now', ?)
        ''', (f'-{max_age_days} days',))
        return cursor.rowcount


def _row_to_intercept(row) -> dict:
    """Convert a database row to an intercept dict."""
    return {
        'id': row['id'],
        'agent_id': row['agent_id'],
        'agent_name': row['agent_name'],
        'mode': row['mode'],
        'started_at': row['started_at'],
        'ended_at': row['ended_at'],
        'latitude': row['latitude'],
        'longitude': row['longitude'],
        'location_label': row['location_label'],
        'summary': row['summary'],
        'decoded_output': json.loads(row['decoded_output']) if row['decoded_output'] else None,
        'raw_artifact_path': row['raw_artifact_path'],
        'retention_policy': row['retention_policy'],
        'metadata': json.loads(row['metadata']) if row['metadata'] else None,
        'created_at': row['created_at'],
    }


# =============================================================================
# Task CRUD
# =============================================================================

def create_task(
    task_id: str,
    mode: str,
    agent_id: int | None = None,
    agent_name: str | None = None,
    lease_id: str | None = None,
    params: dict | None = None,
) -> str:
    """
    Create a task record.

    Returns:
        Task ID
    """
    from utils.database import get_db
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        conn.execute('''
            INSERT INTO tasks (id, mode, agent_id, agent_name, lease_id, state, params, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            task_id, mode, agent_id, agent_name, lease_id,
            TaskState.PENDING.value,
            json.dumps(params) if params else None,
            now,
        ))
    return task_id


def update_task_state(
    task_id: str,
    state: TaskState,
    result_summary: str | None = None,
    error_message: str | None = None,
    artifact_ids: list[int] | None = None,
) -> bool:
    """Update a task's state and optional result fields."""
    from utils.database import get_db
    now = datetime.now(timezone.utc).isoformat()

    updates = ['state = ?']
    params: list = [state.value]

    # Set timestamp based on state
    if state == TaskState.ASSIGNED:
        updates.append('assigned_at = ?')
        params.append(now)
    elif state == TaskState.RUNNING:
        updates.append('started_at = ?')
        params.append(now)
    elif state in (TaskState.COMPLETED, TaskState.FAILED, TaskState.CANCELLED):
        updates.append('completed_at = ?')
        params.append(now)

    if result_summary is not None:
        updates.append('result_summary = ?')
        params.append(result_summary)
    if error_message is not None:
        updates.append('error_message = ?')
        params.append(error_message)
    if artifact_ids is not None:
        updates.append('artifact_ids = ?')
        params.append(json.dumps(artifact_ids))

    params.append(task_id)

    with get_db() as conn:
        cursor = conn.execute(
            f'UPDATE tasks SET {", ".join(updates)} WHERE id = ?', params
        )
        return cursor.rowcount > 0


def get_task(task_id: str) -> dict | None:
    """Get a task by ID."""
    from utils.database import get_db
    with get_db() as conn:
        cursor = conn.execute('SELECT * FROM tasks WHERE id = ?', (task_id,))
        row = cursor.fetchone()
        return _row_to_task(row) if row else None


def list_tasks(
    state: TaskState | None = None,
    agent_id: int | None = None,
    mode: str | None = None,
    limit: int = 50,
) -> list[dict]:
    """List tasks with optional filters."""
    from utils.database import get_db
    conditions = []
    params: list = []

    if state:
        conditions.append('state = ?')
        params.append(state.value)
    if agent_id is not None:
        conditions.append('agent_id = ?')
        params.append(agent_id)
    if mode:
        conditions.append('mode = ?')
        params.append(mode)

    where = f'WHERE {" AND ".join(conditions)}' if conditions else ''
    params.append(limit)

    with get_db() as conn:
        cursor = conn.execute(f'''
            SELECT * FROM tasks {where}
            ORDER BY created_at DESC LIMIT ?
        ''', params)
        return [_row_to_task(row) for row in cursor]


def _row_to_task(row) -> dict:
    """Convert a database row to a task dict."""
    return {
        'id': row['id'],
        'mode': row['mode'],
        'agent_id': row['agent_id'],
        'agent_name': row['agent_name'],
        'lease_id': row['lease_id'],
        'state': row['state'],
        'params': json.loads(row['params']) if row['params'] else None,
        'result_summary': row['result_summary'],
        'artifact_ids': json.loads(row['artifact_ids']) if row['artifact_ids'] else None,
        'created_at': row['created_at'],
        'assigned_at': row['assigned_at'],
        'started_at': row['started_at'],
        'completed_at': row['completed_at'],
        'error_message': row['error_message'],
    }


# =============================================================================
# Agent Buffer (store-and-forward)
# =============================================================================

def store_buffered_payload(
    agent_id: int,
    agent_name: str | None,
    scan_type: str,
    payload: dict,
    buffered_at: str | None = None,
) -> int:
    """
    Store a buffered payload from an agent (for partition recovery).

    Returns:
        Buffer record ID
    """
    from utils.database import get_db
    ts = buffered_at or datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        cursor = conn.execute('''
            INSERT INTO agent_buffers (agent_id, agent_name, scan_type, payload, buffered_at)
            VALUES (?, ?, ?, ?, ?)
        ''', (agent_id, agent_name, scan_type, json.dumps(payload), ts))
        return cursor.lastrowid


def get_unsynced_buffers(
    agent_id: int | None = None,
    limit: int = 100,
) -> list[dict]:
    """Get buffered payloads that haven't been synced yet."""
    from utils.database import get_db
    conditions = ['synced_at IS NULL']
    params: list = []

    if agent_id is not None:
        conditions.append('agent_id = ?')
        params.append(agent_id)

    where = f'WHERE {" AND ".join(conditions)}'
    params.append(limit)

    with get_db() as conn:
        cursor = conn.execute(f'''
            SELECT * FROM agent_buffers {where}
            ORDER BY buffered_at ASC LIMIT ?
        ''', params)
        return [_row_to_buffer(row) for row in cursor]


def mark_buffer_synced(buffer_id: int) -> bool:
    """Mark a buffered payload as synced."""
    from utils.database import get_db
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        cursor = conn.execute(
            'UPDATE agent_buffers SET synced_at = ? WHERE id = ?',
            (now, buffer_id)
        )
        return cursor.rowcount > 0


def cleanup_old_buffers(max_age_hours: int = 72) -> int:
    """Remove old synced buffers."""
    from utils.database import get_db
    with get_db() as conn:
        cursor = conn.execute('''
            DELETE FROM agent_buffers
            WHERE synced_at IS NOT NULL
              AND synced_at < datetime('now', ?)
        ''', (f'-{max_age_hours} hours',))
        return cursor.rowcount


def _row_to_buffer(row) -> dict:
    """Convert a database row to a buffer dict."""
    return {
        'id': row['id'],
        'agent_id': row['agent_id'],
        'agent_name': row['agent_name'],
        'scan_type': row['scan_type'],
        'payload': json.loads(row['payload']) if row['payload'] else None,
        'buffered_at': row['buffered_at'],
        'synced_at': row['synced_at'],
        'retry_count': row['retry_count'],
    }
