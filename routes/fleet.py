"""
Fleet API routes for the remote-agent-first architecture.

Provides endpoints for:
    - Fleet capability summary (drives the frontend)
    - Agent selection / task routing
    - Resource lease management
    - Historical intercept search
    - Task lifecycle
    - Agent buffered data sync
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone

from flask import Blueprint, jsonify, request

from utils.capability_aggregator import CapabilityState, fleet_aggregator
from utils.history_store import (
    TaskState,
    cleanup_old_buffers,
    count_intercepts,
    create_task,
    get_task,
    get_unsynced_buffers,
    list_tasks,
    mark_buffer_synced,
    search_intercepts,
    store_buffered_payload,
    store_intercept,
    update_task_state,
)
from utils.resource_manager import resource_manager
from utils.responses import api_error

logger = logging.getLogger('intercept.fleet')

fleet_bp = Blueprint('fleet', __name__, url_prefix='/fleet')


# =============================================================================
# Fleet Capabilities (drives the frontend)
# =============================================================================

@fleet_bp.route('/capabilities')
def get_fleet_capabilities():
    """
    Get the aggregated fleet capability model.

    This is the primary endpoint the frontend uses to determine which
    modes to show, which to disable, and which to hide entirely.

    Response:
    {
        "capabilities": [...],
        "summary": {
            "agent_count": N,
            "online_count": N,
            "total_capabilities": N,
            "available_capabilities": N,
        }
    }
    """
    fleet_aggregator.sync_from_database()
    summary = fleet_aggregator.get_fleet_summary()

    return jsonify({
        'status': 'success',
        'capabilities': summary.get('capability_groups', []),
        'agents': summary.get('agents', []),
        'agent_count': summary.get('agent_count', 0),
        'online_count': summary.get('online_count', 0),
        'total_capabilities': summary.get('total_capabilities', 0),
        'available_capabilities': summary.get('available_capabilities', 0),
        'timestamp': datetime.now(timezone.utc).isoformat(),
    })


@fleet_bp.route('/capabilities/<mode>')
def get_mode_capability(mode: str):
    """
    Get detail for a specific capability including supporting agents.

    Response includes which agents support the mode and their status.
    """
    fleet_aggregator.sync_from_database()
    state = fleet_aggregator.get_capability_state(mode)
    agents = fleet_aggregator.get_agents_for_mode(mode)

    return jsonify({
        'status': 'success',
        'mode': mode,
        'state': state.value,
        'agents': agents,
    })


@fleet_bp.route('/capabilities/<mode>/select-agent')
def select_agent_for_mode(mode: str):
    """
    Select the best available agent for a given mode.

    Simple first-available strategy. The frontend can also allow
    manual agent selection.
    """
    fleet_aggregator.sync_from_database()
    agent = fleet_aggregator.select_agent_for_mode(mode)

    if not agent:
        state = fleet_aggregator.get_capability_state(mode)
        if state == CapabilityState.UNSUPPORTED:
            return api_error(f'No agent supports mode: {mode}', 404)
        elif state == CapabilityState.OFFLINE:
            return api_error(f'All agents supporting {mode} are offline', 503)
        else:
            return api_error(f'All agents supporting {mode} are busy', 409)

    return jsonify({
        'status': 'success',
        'mode': mode,
        'agent': agent,
    })


# =============================================================================
# Resource Leases
# =============================================================================

@fleet_bp.route('/resources')
def get_resources():
    """Get all registered resources and their states."""
    return jsonify({
        'status': 'success',
        **resource_manager.get_summary(),
    })


@fleet_bp.route('/resources/<int:agent_id>')
def get_agent_resources(agent_id: int):
    """Get resources for a specific agent."""
    resources = resource_manager.get_agent_resources(agent_id)
    return jsonify({
        'status': 'success',
        'agent_id': agent_id,
        'resources': resources,
    })


@fleet_bp.route('/leases', methods=['GET'])
def get_leases():
    """Get all active resource leases."""
    leases = resource_manager.get_active_leases()
    return jsonify({
        'status': 'success',
        'leases': leases,
        'count': len(leases),
    })


@fleet_bp.route('/leases', methods=['POST'])
def acquire_lease():
    """
    Acquire a lease on a resource.

    Expected JSON:
    {
        "agent_id": 1,
        "device_id": "0",
        "mode": "adsb",
        "task_id": "optional-task-id",
        "ttl": 300
    }
    """
    data = request.json or {}

    agent_id = data.get('agent_id')
    device_id = data.get('device_id')
    mode = data.get('mode')

    if not all([agent_id, device_id, mode]):
        return api_error('agent_id, device_id, and mode are required', 400)

    lease = resource_manager.acquire_lease(
        agent_id=int(agent_id),
        device_id=str(device_id),
        mode=mode,
        task_id=data.get('task_id'),
        ttl=data.get('ttl'),
    )

    if not lease:
        return api_error('Resource is not available', 409)

    return jsonify({
        'status': 'success',
        'lease': lease.to_dict(),
    }), 201


@fleet_bp.route('/leases/<lease_id>/renew', methods=['POST'])
def renew_lease(lease_id: str):
    """Renew an active lease."""
    data = request.json or {}
    ttl = data.get('ttl')

    if resource_manager.renew_lease(lease_id, ttl=ttl):
        lease_info = resource_manager.get_lease(lease_id)
        return jsonify({'status': 'success', 'lease': lease_info})
    else:
        return api_error('Lease not found or not active', 404)


@fleet_bp.route('/leases/<lease_id>', methods=['DELETE'])
def release_lease(lease_id: str):
    """Release a lease."""
    if resource_manager.release_lease(lease_id):
        return jsonify({'status': 'success', 'message': 'Lease released'})
    else:
        return api_error('Lease not found', 404)


# =============================================================================
# Task Lifecycle
# =============================================================================

@fleet_bp.route('/tasks', methods=['GET'])
def get_tasks():
    """
    List tasks with optional filters.

    Query params: state, agent_id, mode, limit
    """
    state_str = request.args.get('state')
    state = None
    if state_str:
        try:
            state = TaskState(state_str)
        except ValueError:
            return api_error(f'Invalid state: {state_str}', 400)
    agent_id = request.args.get('agent_id', type=int)
    mode = request.args.get('mode')
    limit = request.args.get('limit', 50, type=int)

    tasks = list_tasks(state=state, agent_id=agent_id, mode=mode, limit=min(limit, 500))

    return jsonify({
        'status': 'success',
        'tasks': tasks,
        'count': len(tasks),
    })


@fleet_bp.route('/tasks', methods=['POST'])
def create_new_task():
    """
    Create a new task and optionally route it to an agent.

    Expected JSON:
    {
        "mode": "adsb",
        "params": {...},
        "agent_id": null  (optional, auto-select if null)
    }
    """
    data = request.json or {}
    mode = data.get('mode')
    if not mode:
        return api_error('mode is required', 400)

    task_id = str(uuid.uuid4())
    agent_id = data.get('agent_id')
    agent_name = None

    # Auto-select agent if not specified
    if not agent_id:
        fleet_aggregator.sync_from_database()
        selected = fleet_aggregator.select_agent_for_mode(mode)
        if selected:
            agent_id = selected['agent_id']
            agent_name = selected['agent_name']
        # Not finding an agent is ok - task stays pending

    if agent_id and not agent_name:
        from utils.database import get_agent
        agent_record = get_agent(int(agent_id))
        if agent_record:
            agent_name = agent_record['name']

    create_task(
        task_id=task_id,
        mode=mode,
        agent_id=int(agent_id) if agent_id else None,
        agent_name=agent_name,
        params=data.get('params'),
    )

    # If agent assigned, mark as assigned
    if agent_id:
        update_task_state(task_id, TaskState.ASSIGNED)

    task = get_task(task_id)
    return jsonify({
        'status': 'success',
        'task': task,
    }), 201


@fleet_bp.route('/tasks/<task_id>', methods=['GET'])
def get_task_detail(task_id: str):
    """Get task details."""
    task = get_task(task_id)
    if not task:
        return api_error('Task not found', 404)
    return jsonify({'status': 'success', 'task': task})


@fleet_bp.route('/tasks/<task_id>/state', methods=['PUT'])
def update_task(task_id: str):
    """
    Update a task's state.

    Expected JSON:
    {
        "state": "running" | "completed" | "failed" | "cancelled",
        "result_summary": "optional summary",
        "error_message": "optional error"
    }
    """
    data = request.json or {}
    state_str = data.get('state')
    if not state_str:
        return api_error('state is required', 400)

    try:
        state = TaskState(state_str)
    except ValueError:
        return api_error(f'Invalid state: {state_str}', 400)

    if update_task_state(
        task_id,
        state,
        result_summary=data.get('result_summary'),
        error_message=data.get('error_message'),
        artifact_ids=data.get('artifact_ids'),
    ):
        task = get_task(task_id)
        return jsonify({'status': 'success', 'task': task})
    else:
        return api_error('Task not found', 404)


# =============================================================================
# Historical Intercepts
# =============================================================================

@fleet_bp.route('/history')
def get_history():
    """
    Search intercept history.

    Query params: mode, agent_id, since, until, limit, offset
    """
    mode = request.args.get('mode')
    agent_id = request.args.get('agent_id', type=int)
    since = request.args.get('since')
    until = request.args.get('until')
    limit = request.args.get('limit', 100, type=int)
    offset = request.args.get('offset', 0, type=int)

    records = search_intercepts(
        mode=mode,
        agent_id=agent_id,
        since=since,
        until=until,
        limit=min(limit, 500),
        offset=offset,
    )

    total = count_intercepts(mode=mode, agent_id=agent_id)

    return jsonify({
        'status': 'success',
        'records': records,
        'count': len(records),
        'total': total,
    })


@fleet_bp.route('/history', methods=['POST'])
def store_history():
    """
    Store an intercept record.

    Expected JSON:
    {
        "mode": "adsb",
        "agent_id": 1,
        "agent_name": "node-1",
        "started_at": "2024-01-15T10:00:00Z",
        "ended_at": "2024-01-15T10:30:00Z",
        "summary": "Decoded 42 aircraft",
        "decoded_output": [...],
        "retention_policy": "standard"
    }
    """
    data = request.json or {}
    mode = data.get('mode')
    if not mode:
        return api_error('mode is required', 400)

    record_id = store_intercept(
        mode=mode,
        agent_id=data.get('agent_id'),
        agent_name=data.get('agent_name'),
        started_at=data.get('started_at'),
        ended_at=data.get('ended_at'),
        latitude=data.get('latitude'),
        longitude=data.get('longitude'),
        location_label=data.get('location_label'),
        summary=data.get('summary'),
        decoded_output=data.get('decoded_output'),
        raw_artifact_path=data.get('raw_artifact_path'),
        retention_policy=data.get('retention_policy', 'standard'),
        metadata=data.get('metadata'),
    )

    return jsonify({
        'status': 'success',
        'record_id': record_id,
    }), 201


# =============================================================================
# Agent Buffer Sync (store-and-forward)
# =============================================================================

@fleet_bp.route('/buffer/sync', methods=['POST'])
def sync_agent_buffer():
    """
    Receive buffered data from an agent after a network partition.

    Expected JSON:
    {
        "agent_id": 1,
        "agent_name": "node-1",
        "payloads": [
            {
                "scan_type": "adsb",
                "payload": {...},
                "buffered_at": "2024-01-15T10:00:00Z"
            }
        ]
    }
    """
    data = request.json or {}
    agent_id = data.get('agent_id')
    if not agent_id:
        return api_error('agent_id is required', 400)

    payloads = data.get('payloads', [])
    stored = 0

    for item in payloads:
        try:
            buffer_id = store_buffered_payload(
                agent_id=int(agent_id),
                agent_name=data.get('agent_name'),
                scan_type=item.get('scan_type', 'unknown'),
                payload=item.get('payload', {}),
                buffered_at=item.get('buffered_at'),
            )
            # Mark as synced immediately since we've received it
            mark_buffer_synced(buffer_id)
            stored += 1
        except Exception as e:
            logger.warning("Failed to store buffered payload: %s", e)

    return jsonify({
        'status': 'success',
        'stored': stored,
        'total': len(payloads),
    }), 202


@fleet_bp.route('/buffer/pending')
def get_pending_buffers():
    """Get unsynced buffered payloads."""
    agent_id = request.args.get('agent_id', type=int)
    limit = request.args.get('limit', 100, type=int)

    buffers = get_unsynced_buffers(agent_id=agent_id, limit=min(limit, 500))

    return jsonify({
        'status': 'success',
        'buffers': buffers,
        'count': len(buffers),
    })


@fleet_bp.route('/buffer/cleanup', methods=['POST'])
def cleanup_buffers():
    """Clean up old synced buffers."""
    data = request.json or {}
    max_age_hours = data.get('max_age_hours', 72)
    count = cleanup_old_buffers(max_age_hours=int(max_age_hours))
    return jsonify({'status': 'success', 'cleaned': count})
