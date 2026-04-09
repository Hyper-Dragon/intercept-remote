"""Tests for the ResourceManager."""

import time

import pytest

from utils.resource_manager import (
    LeaseState,
    ResourceManager,
)


@pytest.fixture
def manager():
    return ResourceManager(default_ttl=10.0)


class TestResourceRegistration:
    """Test resource registration."""

    def test_register_resource(self, manager):
        key = manager.register_resource(
            agent_id=1, device_id='0',
            device_type='rtlsdr',
            device_info={'serial': 'ABC123'},
        )
        assert key == '1:0'
        resource = manager.get_resource(key)
        assert resource is not None
        assert resource['state'] == 'free'
        assert resource['device_type'] == 'rtlsdr'

    def test_unregister_resource(self, manager):
        key = manager.register_resource(agent_id=1, device_id='0', device_type='rtlsdr')
        assert manager.unregister_resource(key)
        assert manager.get_resource(key) is None

    def test_unregister_nonexistent(self, manager):
        assert not manager.unregister_resource('999:0')

    def test_register_agent_resources(self, manager):
        devices = [
            {'device_id': '0', 'type': 'rtlsdr'},
            {'device_id': '1', 'type': 'hackrf'},
        ]
        keys = manager.register_agent_resources(agent_id=1, devices=devices)
        assert len(keys) == 2
        assert '1:0' in keys
        assert '1:1' in keys

    def test_unregister_agent_resources(self, manager):
        manager.register_resource(agent_id=1, device_id='0', device_type='rtlsdr')
        manager.register_resource(agent_id=1, device_id='1', device_type='hackrf')
        manager.register_resource(agent_id=2, device_id='0', device_type='rtlsdr')
        count = manager.unregister_agent_resources(1)
        assert count == 2
        assert manager.get_resource('1:0') is None
        assert manager.get_resource('2:0') is not None

    def test_get_agent_resources(self, manager):
        manager.register_resource(agent_id=1, device_id='0', device_type='rtlsdr')
        manager.register_resource(agent_id=1, device_id='1', device_type='hackrf')
        resources = manager.get_agent_resources(1)
        assert len(resources) == 2

    def test_get_all_resources(self, manager):
        manager.register_resource(agent_id=1, device_id='0', device_type='rtlsdr')
        manager.register_resource(agent_id=2, device_id='0', device_type='hackrf')
        resources = manager.get_all_resources()
        assert len(resources) == 2


class TestLeaseOperations:
    """Test lease acquisition, renewal, and release."""

    def test_acquire_lease(self, manager):
        manager.register_resource(agent_id=1, device_id='0', device_type='rtlsdr')
        lease = manager.acquire_lease(agent_id=1, device_id='0', mode='adsb')
        assert lease is not None
        assert lease.mode == 'adsb'
        assert lease.state == LeaseState.ACTIVE
        assert lease.resource_key == '1:0'

    def test_acquire_lease_busy_resource(self, manager):
        manager.register_resource(agent_id=1, device_id='0', device_type='rtlsdr')
        lease1 = manager.acquire_lease(agent_id=1, device_id='0', mode='adsb')
        assert lease1 is not None
        lease2 = manager.acquire_lease(agent_id=1, device_id='0', mode='pager')
        assert lease2 is None  # Resource is busy

    def test_acquire_lease_unknown_resource(self, manager):
        lease = manager.acquire_lease(agent_id=999, device_id='0', mode='adsb')
        assert lease is None

    def test_release_lease(self, manager):
        manager.register_resource(agent_id=1, device_id='0', device_type='rtlsdr')
        lease = manager.acquire_lease(agent_id=1, device_id='0', mode='adsb')
        assert manager.release_lease(lease.lease_id)

        resource = manager.get_resource('1:0')
        assert resource['state'] == 'free'

    def test_release_nonexistent_lease(self, manager):
        assert not manager.release_lease('nonexistent-id')

    def test_renew_lease(self, manager):
        manager.register_resource(agent_id=1, device_id='0', device_type='rtlsdr')
        lease = manager.acquire_lease(agent_id=1, device_id='0', mode='adsb', ttl=5)
        assert manager.renew_lease(lease.lease_id, ttl=60)
        lease_info = manager.get_lease(lease.lease_id)
        assert lease_info is not None

    def test_renew_nonexistent_lease(self, manager):
        assert not manager.renew_lease('nonexistent-id')

    def test_lease_expiry(self, manager):
        manager.register_resource(agent_id=1, device_id='0', device_type='rtlsdr')
        lease = manager.acquire_lease(agent_id=1, device_id='0', mode='adsb', ttl=0.01)
        time.sleep(0.02)
        assert lease.is_expired()

    def test_acquire_after_expiry(self, manager):
        manager.register_resource(agent_id=1, device_id='0', device_type='rtlsdr')
        manager.acquire_lease(agent_id=1, device_id='0', mode='adsb', ttl=0.01)
        time.sleep(0.02)
        # Should be able to acquire again after expiry
        lease2 = manager.acquire_lease(agent_id=1, device_id='0', mode='pager')
        assert lease2 is not None
        assert lease2.mode == 'pager'

    def test_is_resource_free(self, manager):
        manager.register_resource(agent_id=1, device_id='0', device_type='rtlsdr')
        assert manager.is_resource_free(1, '0')
        manager.acquire_lease(agent_id=1, device_id='0', mode='adsb')
        assert not manager.is_resource_free(1, '0')

    def test_is_resource_free_after_expiry(self, manager):
        manager.register_resource(agent_id=1, device_id='0', device_type='rtlsdr')
        manager.acquire_lease(agent_id=1, device_id='0', mode='adsb', ttl=0.01)
        time.sleep(0.02)
        assert manager.is_resource_free(1, '0')

    def test_is_resource_free_nonexistent(self, manager):
        assert not manager.is_resource_free(999, '0')

    def test_get_active_leases(self, manager):
        manager.register_resource(agent_id=1, device_id='0', device_type='rtlsdr')
        manager.register_resource(agent_id=1, device_id='1', device_type='hackrf')
        manager.acquire_lease(agent_id=1, device_id='0', mode='adsb')
        manager.acquire_lease(agent_id=1, device_id='1', mode='pager')
        leases = manager.get_active_leases()
        assert len(leases) == 2


class TestLeaseMaintenance:
    """Test lease cleanup and recovery."""

    def test_cleanup_expired_leases(self, manager):
        manager.register_resource(agent_id=1, device_id='0', device_type='rtlsdr')
        manager.acquire_lease(agent_id=1, device_id='0', mode='adsb', ttl=0.01)
        time.sleep(0.02)
        cleaned = manager.cleanup_expired_leases()
        assert cleaned == 1

        resource = manager.get_resource('1:0')
        assert resource['state'] == 'free'

    def test_cleanup_does_not_touch_active(self, manager):
        manager.register_resource(agent_id=1, device_id='0', device_type='rtlsdr')
        manager.acquire_lease(agent_id=1, device_id='0', mode='adsb', ttl=300)
        cleaned = manager.cleanup_expired_leases()
        assert cleaned == 0

    def test_unregister_agent_abandons_leases(self, manager):
        manager.register_resource(agent_id=1, device_id='0', device_type='rtlsdr')
        manager.acquire_lease(agent_id=1, device_id='0', mode='adsb')
        manager.unregister_agent_resources(1)
        # Resource should be gone
        assert manager.get_resource('1:0') is None

    def test_lease_with_task_id(self, manager):
        manager.register_resource(agent_id=1, device_id='0', device_type='rtlsdr')
        lease = manager.acquire_lease(
            agent_id=1, device_id='0', mode='adsb', task_id='task-123'
        )
        assert lease.task_id == 'task-123'


class TestSummary:
    """Test resource manager summary."""

    def test_summary(self, manager):
        manager.register_resource(agent_id=1, device_id='0', device_type='rtlsdr')
        manager.register_resource(agent_id=1, device_id='1', device_type='hackrf')
        manager.acquire_lease(agent_id=1, device_id='0', mode='adsb')

        summary = manager.get_summary()
        assert summary['total_resources'] == 2
        assert summary['free_resources'] == 1
        assert summary['busy_resources'] == 1
        assert summary['active_leases'] == 1
