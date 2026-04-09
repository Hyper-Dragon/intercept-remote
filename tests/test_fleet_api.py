"""Tests for the Fleet API endpoints."""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils.capability_aggregator import fleet_aggregator

# =============================================================================
# Fixtures
# =============================================================================

@pytest.fixture
def setup_db(tmp_path):
    """Set up a temporary database."""
    import utils.database as db_module
    from utils.database import init_db

    test_db_path = tmp_path / 'test.db'
    original_db_path = db_module.DB_PATH
    original_db_dir = db_module.DB_DIR
    db_module.DB_PATH = test_db_path
    db_module.DB_DIR = tmp_path

    if hasattr(db_module._local, 'connection') and db_module._local.connection:
        db_module._local.connection.close()
        db_module._local.connection = None

    init_db()

    yield

    if hasattr(db_module._local, 'connection') and db_module._local.connection:
        db_module._local.connection.close()
        db_module._local.connection = None
    db_module.DB_PATH = original_db_path
    db_module.DB_DIR = original_db_dir


@pytest.fixture
def app(setup_db):
    """Create Flask app with fleet blueprint."""
    from flask import Flask

    from routes.fleet import fleet_bp

    app = Flask(__name__)
    app.config['TESTING'] = True
    app.register_blueprint(fleet_bp)
    return app


@pytest.fixture
def client(app):
    """Create test client."""
    # Reset the fleet aggregator between tests
    fleet_aggregator._agents.clear()
    fleet_aggregator._fleet_model = []
    return app.test_client()


@pytest.fixture
def seeded_client(client, setup_db):
    """Client with agents pre-registered in the aggregator."""
    from utils.database import create_agent, update_agent

    # Create agents in database
    agent1_id = create_agent(
        name='node-1',
        base_url='http://192.168.1.10:8020',
        capabilities={'adsb': True, 'pager': True, 'sensor': True},
        interfaces={'sdr_devices': [{'device_id': '0', 'type': 'rtlsdr'}]},
    )
    update_agent(agent1_id, update_last_seen=True)

    agent2_id = create_agent(
        name='node-2',
        base_url='http://192.168.1.11:8020',
        capabilities={'adsb': True, 'wifi': True},
        interfaces={'sdr_devices': [{'device_id': '0', 'type': 'rtlsdr'}]},
    )
    update_agent(agent2_id, update_last_seen=True)

    # Seed the aggregator
    fleet_aggregator.update_agent(
        agent_id=agent1_id, agent_name='node-1',
        capabilities={'adsb': True, 'pager': True, 'sensor': True},
        is_online=True,
    )
    fleet_aggregator.update_agent(
        agent_id=agent2_id, agent_name='node-2',
        capabilities={'adsb': True, 'wifi': True},
        is_online=True,
    )

    return client


class TestFleetCapabilities:
    """Test /fleet/capabilities endpoints."""

    def test_get_capabilities_empty_fleet(self, client):
        resp = client.get('/fleet/capabilities')
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['status'] == 'success'
        assert data['agent_count'] == 0

    def test_get_capabilities_with_agents(self, seeded_client):
        resp = seeded_client.get('/fleet/capabilities')
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['status'] == 'success'
        assert data['agent_count'] >= 2
        assert data['online_count'] >= 1
        assert data['total_capabilities'] >= 1
        assert 'capabilities' in data

    def test_get_mode_capability(self, seeded_client):
        resp = seeded_client.get('/fleet/capabilities/adsb')
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['mode'] == 'adsb'
        assert data['state'] in ('available', 'busy', 'offline')
        assert len(data['agents']) >= 1

    def test_get_mode_capability_unsupported(self, seeded_client):
        resp = seeded_client.get('/fleet/capabilities/nonexistent')
        data = resp.get_json()
        assert data['state'] == 'unsupported'

    def test_select_agent_for_mode(self, seeded_client):
        resp = seeded_client.get('/fleet/capabilities/adsb/select-agent')
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['status'] == 'success'
        assert data['agent'] is not None
        assert 'agent_id' in data['agent']

    def test_select_agent_unsupported_mode(self, seeded_client):
        resp = seeded_client.get('/fleet/capabilities/nonexistent/select-agent')
        assert resp.status_code == 404


class TestResourceLeases:
    """Test /fleet/resources and /fleet/leases endpoints."""

    def test_get_resources_empty(self, client):
        resp = client.get('/fleet/resources')
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['total_resources'] == 0

    def test_get_leases_empty(self, client):
        resp = client.get('/fleet/leases')
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['count'] == 0

    def test_acquire_lease_missing_fields(self, client):
        resp = client.post('/fleet/leases', json={'agent_id': 1})
        assert resp.status_code == 400

    def test_acquire_lease_no_resource(self, client):
        resp = client.post('/fleet/leases', json={
            'agent_id': 1, 'device_id': '0', 'mode': 'adsb'
        })
        assert resp.status_code == 409

    def test_release_nonexistent_lease(self, client):
        resp = client.delete('/fleet/leases/nonexistent-id')
        assert resp.status_code == 404


class TestTasks:
    """Test /fleet/tasks endpoints."""

    def test_list_tasks_empty(self, client):
        resp = client.get('/fleet/tasks')
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['count'] == 0

    def test_create_task(self, seeded_client):
        resp = seeded_client.post('/fleet/tasks', json={
            'mode': 'adsb',
            'params': {'gain': 40},
        })
        assert resp.status_code == 201
        data = resp.get_json()
        assert data['status'] == 'success'
        assert data['task']['mode'] == 'adsb'
        assert data['task']['state'] in ('pending', 'assigned')

    def test_create_task_missing_mode(self, client):
        resp = client.post('/fleet/tasks', json={'params': {}})
        assert resp.status_code == 400

    def test_get_task_not_found(self, client):
        resp = client.get('/fleet/tasks/nonexistent-id')
        assert resp.status_code == 404

    def test_update_task_state(self, seeded_client):
        # Create a task first
        resp = seeded_client.post('/fleet/tasks', json={'mode': 'adsb'})
        task_id = resp.get_json()['task']['id']

        # Update to running
        resp = seeded_client.put(f'/fleet/tasks/{task_id}/state', json={
            'state': 'running'
        })
        assert resp.status_code == 200
        assert resp.get_json()['task']['state'] == 'running'

    def test_update_task_invalid_state(self, seeded_client):
        resp = seeded_client.post('/fleet/tasks', json={'mode': 'adsb'})
        task_id = resp.get_json()['task']['id']

        resp = seeded_client.put(f'/fleet/tasks/{task_id}/state', json={
            'state': 'invalid_state'
        })
        assert resp.status_code == 400


class TestHistory:
    """Test /fleet/history endpoints."""

    def test_get_history_empty(self, client):
        resp = client.get('/fleet/history')
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['count'] == 0

    def test_store_history(self, seeded_client):
        resp = seeded_client.post('/fleet/history', json={
            'mode': 'adsb',
            'agent_name': 'node-1',
            'summary': 'Decoded 42 aircraft',
        })
        assert resp.status_code == 201
        data = resp.get_json()
        assert data['record_id'] >= 1

    def test_store_history_missing_mode(self, client):
        resp = client.post('/fleet/history', json={'summary': 'test'})
        assert resp.status_code == 400

    def test_search_history_by_mode(self, seeded_client):
        # Store two records
        seeded_client.post('/fleet/history', json={
            'mode': 'adsb', 'summary': 'Aircraft capture'
        })
        seeded_client.post('/fleet/history', json={
            'mode': 'pager', 'summary': 'Pager capture'
        })

        # Search by mode
        resp = seeded_client.get('/fleet/history?mode=adsb')
        data = resp.get_json()
        assert data['count'] == 1
        assert data['records'][0]['mode'] == 'adsb'


class TestBufferSync:
    """Test /fleet/buffer endpoints."""

    def test_sync_buffer(self, seeded_client):
        resp = seeded_client.post('/fleet/buffer/sync', json={
            'agent_id': 1,
            'agent_name': 'node-1',
            'payloads': [
                {'scan_type': 'adsb', 'payload': {'aircraft': 'TEST123'}},
                {'scan_type': 'sensor', 'payload': {'temp': 22.5}},
            ]
        })
        assert resp.status_code == 202
        data = resp.get_json()
        assert data['stored'] == 2

    def test_sync_buffer_missing_agent(self, client):
        resp = client.post('/fleet/buffer/sync', json={'payloads': []})
        assert resp.status_code == 400

    def test_get_pending_buffers(self, client):
        resp = client.get('/fleet/buffer/pending')
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['count'] == 0
