"""Tests for the CapabilityAggregator."""

import pytest

from utils.capability_aggregator import (
    GROUP_DISPLAY_MAP,
    MODE_DISPLAY_MAP,
    CapabilityAggregator,
    CapabilityState,
    FleetCapability,
)


@pytest.fixture
def aggregator():
    return CapabilityAggregator()


class TestAgentRegistration:
    """Test agent data management in the aggregator."""

    def test_update_agent_adds_to_fleet(self, aggregator):
        aggregator.update_agent(
            agent_id=1,
            agent_name='node-1',
            capabilities={'adsb': True, 'sensor': True},
            is_online=True,
        )
        caps = aggregator.get_fleet_capabilities()
        modes = {c['mode'] for c in caps}
        assert 'adsb' in modes
        assert 'sensor' in modes

    def test_update_agent_replaces_existing(self, aggregator):
        aggregator.update_agent(
            agent_id=1, agent_name='node-1',
            capabilities={'adsb': True, 'sensor': True},
            is_online=True,
        )
        aggregator.update_agent(
            agent_id=1, agent_name='node-1',
            capabilities={'adsb': True},
            is_online=True,
        )
        caps = aggregator.get_fleet_capabilities()
        modes = {c['mode'] for c in caps}
        assert 'adsb' in modes
        assert 'sensor' not in modes

    def test_remove_agent(self, aggregator):
        aggregator.update_agent(
            agent_id=1, agent_name='node-1',
            capabilities={'adsb': True},
            is_online=True,
        )
        aggregator.remove_agent(1)
        caps = aggregator.get_fleet_capabilities()
        assert len(caps) == 0

    def test_remove_nonexistent_agent(self, aggregator):
        # Should not raise
        aggregator.remove_agent(999)

    def test_mark_agent_offline(self, aggregator):
        aggregator.update_agent(
            agent_id=1, agent_name='node-1',
            capabilities={'adsb': True},
            is_online=True,
        )
        aggregator.mark_agent_offline(1)
        state = aggregator.get_capability_state('adsb')
        assert state == CapabilityState.OFFLINE

    def test_mark_agent_online(self, aggregator):
        aggregator.update_agent(
            agent_id=1, agent_name='node-1',
            capabilities={'adsb': True},
            is_online=False,
        )
        assert aggregator.get_capability_state('adsb') == CapabilityState.OFFLINE
        aggregator.mark_agent_online(1)
        assert aggregator.get_capability_state('adsb') == CapabilityState.AVAILABLE


class TestFleetCapabilityModel:
    """Test fleet capability aggregation logic."""

    def test_empty_fleet(self, aggregator):
        caps = aggregator.get_fleet_capabilities()
        assert caps == []

    def test_single_agent_capabilities(self, aggregator):
        aggregator.update_agent(
            agent_id=1, agent_name='node-1',
            capabilities={'adsb': True, 'pager': True, 'sensor': False},
            is_online=True,
        )
        caps = aggregator.get_fleet_capabilities()
        modes = {c['mode'] for c in caps}
        # sensor=False should not appear
        assert 'adsb' in modes
        assert 'pager' in modes
        assert 'sensor' not in modes

    def test_multiple_agents_same_capability(self, aggregator):
        aggregator.update_agent(
            agent_id=1, agent_name='node-1',
            capabilities={'adsb': True},
            is_online=True,
        )
        aggregator.update_agent(
            agent_id=2, agent_name='node-2',
            capabilities={'adsb': True},
            is_online=True,
        )
        caps = aggregator.get_fleet_capabilities()
        adsb = next(c for c in caps if c['mode'] == 'adsb')
        assert adsb['total_agent_count'] == 2
        assert adsb['available_agent_count'] == 2
        assert adsb['state'] == 'available'

    def test_all_agents_offline_shows_offline(self, aggregator):
        aggregator.update_agent(
            agent_id=1, agent_name='node-1',
            capabilities={'adsb': True},
            is_online=False,
        )
        state = aggregator.get_capability_state('adsb')
        assert state == CapabilityState.OFFLINE

    def test_busy_state_when_running(self, aggregator):
        aggregator.update_agent(
            agent_id=1, agent_name='node-1',
            capabilities={'adsb': True},
            is_online=True,
            running_modes=['adsb'],
        )
        state = aggregator.get_capability_state('adsb')
        assert state == CapabilityState.BUSY

    def test_available_when_one_agent_free(self, aggregator):
        """If one agent is running but another is free, mode is available."""
        aggregator.update_agent(
            agent_id=1, agent_name='node-1',
            capabilities={'adsb': True},
            is_online=True,
            running_modes=['adsb'],
        )
        aggregator.update_agent(
            agent_id=2, agent_name='node-2',
            capabilities={'adsb': True},
            is_online=True,
            running_modes=[],
        )
        state = aggregator.get_capability_state('adsb')
        assert state == CapabilityState.AVAILABLE

    def test_unsupported_mode(self, aggregator):
        aggregator.update_agent(
            agent_id=1, agent_name='node-1',
            capabilities={'adsb': True},
            is_online=True,
        )
        state = aggregator.get_capability_state('nonexistent_mode')
        assert state == CapabilityState.UNSUPPORTED

    def test_capabilities_include_display_metadata(self, aggregator):
        aggregator.update_agent(
            agent_id=1, agent_name='node-1',
            capabilities={'adsb': True},
            is_online=True,
        )
        caps = aggregator.get_fleet_capabilities()
        adsb = next(c for c in caps if c['mode'] == 'adsb')
        assert adsb['label'] == 'Aircraft'
        assert adsb['group'] == 'tracking'
        assert adsb['icon'] == 'aircraft'

    def test_unknown_mode_gets_default_display(self, aggregator):
        aggregator.update_agent(
            agent_id=1, agent_name='node-1',
            capabilities={'custom_mode': True},
            is_online=True,
        )
        caps = aggregator.get_fleet_capabilities()
        custom = next(c for c in caps if c['mode'] == 'custom_mode')
        assert custom['label'] == 'Custom Mode'
        assert custom['group'] == 'other'


class TestFleetSummary:
    """Test fleet summary generation."""

    def test_summary_counts(self, aggregator):
        aggregator.update_agent(
            agent_id=1, agent_name='node-1',
            capabilities={'adsb': True, 'pager': True},
            is_online=True,
        )
        aggregator.update_agent(
            agent_id=2, agent_name='node-2',
            capabilities={'adsb': True},
            is_online=False,
        )
        summary = aggregator.get_fleet_summary()
        assert summary['agent_count'] == 2
        assert summary['online_count'] == 1
        assert summary['total_capabilities'] == 2  # adsb + pager
        assert summary['available_capabilities'] >= 1

    def test_summary_groups(self, aggregator):
        aggregator.update_agent(
            agent_id=1, agent_name='node-1',
            capabilities={'adsb': True, 'pager': True, 'wifi': True},
            is_online=True,
        )
        summary = aggregator.get_fleet_summary()
        groups = summary['capability_groups']
        group_names = {g['group'] for g in groups}
        assert 'signals' in group_names
        assert 'tracking' in group_names
        assert 'wireless' in group_names


class TestAgentSelection:
    """Test agent selection for mode routing."""

    def test_select_available_agent(self, aggregator):
        aggregator.update_agent(
            agent_id=1, agent_name='node-1',
            capabilities={'adsb': True},
            is_online=True,
        )
        agent = aggregator.select_agent_for_mode('adsb')
        assert agent is not None
        assert agent['agent_id'] == 1
        assert agent['agent_name'] == 'node-1'

    def test_select_skips_busy_agent(self, aggregator):
        aggregator.update_agent(
            agent_id=1, agent_name='node-1',
            capabilities={'adsb': True},
            is_online=True,
            running_modes=['adsb'],
        )
        aggregator.update_agent(
            agent_id=2, agent_name='node-2',
            capabilities={'adsb': True},
            is_online=True,
        )
        agent = aggregator.select_agent_for_mode('adsb')
        assert agent is not None
        assert agent['agent_id'] == 2

    def test_select_returns_none_when_all_busy(self, aggregator):
        aggregator.update_agent(
            agent_id=1, agent_name='node-1',
            capabilities={'adsb': True},
            is_online=True,
            running_modes=['adsb'],
        )
        agent = aggregator.select_agent_for_mode('adsb')
        assert agent is None

    def test_select_returns_none_for_unsupported(self, aggregator):
        agent = aggregator.select_agent_for_mode('nonexistent')
        assert agent is None

    def test_get_agents_for_mode(self, aggregator):
        aggregator.update_agent(
            agent_id=1, agent_name='node-1',
            capabilities={'adsb': True},
            is_online=True,
        )
        aggregator.update_agent(
            agent_id=2, agent_name='node-2',
            capabilities={'adsb': True, 'pager': True},
            is_online=True,
        )
        agents = aggregator.get_agents_for_mode('adsb')
        assert len(agents) == 2

    def test_get_agents_for_mode_sorted_by_availability(self, aggregator):
        aggregator.update_agent(
            agent_id=1, agent_name='node-1',
            capabilities={'adsb': True},
            is_online=True,
            running_modes=['adsb'],
        )
        aggregator.update_agent(
            agent_id=2, agent_name='node-2',
            capabilities={'adsb': True},
            is_online=True,
        )
        agents = aggregator.get_agents_for_mode('adsb')
        # Available (not running) should be first
        assert agents[0]['agent_id'] == 2
        assert agents[1]['agent_id'] == 1


class TestModeDisplayMap:
    """Test that display maps are consistent."""

    def test_all_groups_have_display_metadata(self):
        groups_used = {v['group'] for v in MODE_DISPLAY_MAP.values()}
        for group in groups_used:
            assert group in GROUP_DISPLAY_MAP or group == 'other'

    def test_mode_display_map_has_required_fields(self):
        for mode, display in MODE_DISPLAY_MAP.items():
            assert 'label' in display
            assert 'group' in display
            assert 'icon' in display

    def test_fleet_capability_to_dict(self):
        cap = FleetCapability(
            mode='adsb',
            label='Aircraft',
            group='tracking',
            icon='aircraft',
            state=CapabilityState.AVAILABLE,
        )
        d = cap.to_dict()
        assert d['mode'] == 'adsb'
        assert d['state'] == 'available'
        assert d['label'] == 'Aircraft'
