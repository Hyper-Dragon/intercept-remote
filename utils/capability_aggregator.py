"""
Capability Aggregator for the remote-agent-first architecture.

Aggregates capabilities from all connected remote agents into a unified
fleet capability model. This model is the single source of truth for
the frontend, determining which modes are available, unavailable, or
unsupported.

Architecture:
    - Control plane (this host) does NOT probe local hardware by default.
    - Each agent reports its own capabilities via heartbeat / refresh.
    - This aggregator merges those reports into a live fleet view.
    - The UI reads the aggregated model to decide which modes to show.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum

logger = logging.getLogger('intercept.capability_aggregator')


class CapabilityState(str, Enum):
    """Availability state for a capability across the fleet."""
    AVAILABLE = 'available'       # At least one agent can perform this now
    BUSY = 'busy'                 # Supported but all agents' resources are occupied
    OFFLINE = 'offline'           # Supported by fleet but all supporting agents are offline
    UNSUPPORTED = 'unsupported'   # No agent in the fleet supports this at all


# Canonical mapping from agent mode names to display metadata.
# Order determines UI presentation order within each group.
MODE_DISPLAY_MAP: dict[str, dict] = {
    'pager':          {'label': 'Pager',              'group': 'signals',   'icon': 'pager'},
    'sensor':         {'label': '433 MHz',            'group': 'signals',   'icon': 'sensor'},
    'rtlamr':         {'label': 'Meters',             'group': 'signals',   'icon': 'meter'},
    'subghz':         {'label': 'SubGHz',             'group': 'signals',   'icon': 'subghz'},
    'waterfall':      {'label': 'Waterfall',          'group': 'signals',   'icon': 'waterfall'},
    'morse':          {'label': 'Morse',              'group': 'signals',   'icon': 'morse'},
    'ook':            {'label': 'OOK',                'group': 'signals',   'icon': 'ook'},
    'adsb':           {'label': 'Aircraft',           'group': 'tracking',  'icon': 'aircraft'},
    'ais':            {'label': 'Vessels',             'group': 'tracking',  'icon': 'vessel'},
    'aprs':           {'label': 'APRS',               'group': 'tracking',  'icon': 'aprs'},
    'gps':            {'label': 'GPS',                'group': 'tracking',  'icon': 'gps'},
    'radiosonde':     {'label': 'Radiosonde',         'group': 'tracking',  'icon': 'radiosonde'},
    'acars':          {'label': 'ACARS',              'group': 'radio',     'icon': 'acars'},
    'vdl2':           {'label': 'VDL2',               'group': 'radio',     'icon': 'vdl2'},
    'dsc':            {'label': 'DSC',                'group': 'radio',     'icon': 'dsc'},
    'satellite':      {'label': 'Satellite',          'group': 'space',     'icon': 'satellite'},
    'sstv':           {'label': 'ISS SSTV',           'group': 'space',     'icon': 'sstv'},
    'weather_sat':    {'label': 'Weather Sat',        'group': 'space',     'icon': 'weather_sat'},
    'wefax':          {'label': 'WeFax',              'group': 'space',     'icon': 'wefax'},
    'sstv_general':   {'label': 'HF SSTV',            'group': 'space',     'icon': 'sstv_general'},
    'space_weather':  {'label': 'Space Weather',      'group': 'space',     'icon': 'space_weather'},
    'meteor':         {'label': 'Meteor Scatter',     'group': 'space',     'icon': 'meteor'},
    'wifi':           {'label': 'WiFi',               'group': 'wireless',  'icon': 'wifi'},
    'bluetooth':      {'label': 'Bluetooth',          'group': 'wireless',  'icon': 'bluetooth'},
    'bt_locate':      {'label': 'BT Locate',          'group': 'wireless',  'icon': 'bt_locate'},
    'wifi_locate':    {'label': 'WF Locate',          'group': 'wireless',  'icon': 'wifi_locate'},
    'meshtastic':     {'label': 'Meshtastic',         'group': 'wireless',  'icon': 'meshtastic'},
    'tscm':           {'label': 'TSCM',               'group': 'intel',     'icon': 'tscm'},
    'listening_post': {'label': 'Listening Post',     'group': 'intel',     'icon': 'listening_post'},
    'spy_stations':   {'label': 'Spy Stations',       'group': 'intel',     'icon': 'spy_stations'},
    'websdr':         {'label': 'WebSDR',             'group': 'intel',     'icon': 'websdr'},
    'ground_station': {'label': 'Ground Station',     'group': 'space',     'icon': 'ground_station'},
}

# Group display metadata
GROUP_DISPLAY_MAP: dict[str, dict] = {
    'signals':  {'label': 'Signals',          'order': 1},
    'tracking': {'label': 'Tracking',         'order': 2},
    'radio':    {'label': 'Radio',            'order': 3},
    'space':    {'label': 'Satellite / Space', 'order': 4},
    'wireless': {'label': 'Wireless',         'order': 5},
    'intel':    {'label': 'Intel',            'order': 6},
}

# Modes that don't require SDR hardware (network or software-only)
NETWORK_ONLY_MODES = frozenset({
    'spy_stations', 'websdr', 'space_weather', 'satellite',
})


@dataclass
class AgentCapability:
    """Snapshot of a single agent's reported capability set."""
    agent_id: int
    agent_name: str
    modes: dict[str, bool] = field(default_factory=dict)
    devices: list[dict] = field(default_factory=list)
    interfaces: dict = field(default_factory=dict)
    is_online: bool = False
    last_seen: str | None = None
    running_modes: list[str] = field(default_factory=list)
    gps_coords: dict | None = None


@dataclass
class FleetCapability:
    """Aggregated view of a single capability across the fleet."""
    mode: str
    label: str
    group: str
    icon: str
    state: CapabilityState
    supporting_agents: list[dict] = field(default_factory=list)
    available_agent_count: int = 0
    total_agent_count: int = 0

    def to_dict(self) -> dict:
        return {
            'mode': self.mode,
            'label': self.label,
            'group': self.group,
            'icon': self.icon,
            'state': self.state.value,
            'supporting_agents': self.supporting_agents,
            'available_agent_count': self.available_agent_count,
            'total_agent_count': self.total_agent_count,
        }


class CapabilityAggregator:
    """
    Aggregates agent capabilities into a fleet-wide capability model.

    Thread-safe. The aggregated model is rebuilt whenever agent data
    changes (registration, heartbeat, disconnect).
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._agents: dict[int, AgentCapability] = {}
        self._fleet_model: list[FleetCapability] = []
        self._last_rebuild: float = 0.0
        # Agent considered offline if not seen for this many seconds
        self.agent_offline_threshold_seconds: float = 120.0

    # =========================================================================
    # Agent Data Management
    # =========================================================================

    def update_agent(
        self,
        agent_id: int,
        agent_name: str,
        capabilities: dict | None = None,
        interfaces: dict | None = None,
        is_online: bool = True,
        last_seen: str | None = None,
        running_modes: list[str] | None = None,
        gps_coords: dict | None = None,
    ) -> None:
        """
        Update or register an agent's capability snapshot.

        Args:
            agent_id: Unique agent identifier
            agent_name: Human-readable agent name
            capabilities: Dict of mode_name -> enabled (bool)
            interfaces: Dict with device info (sdr_devices, wifi, bt, etc.)
            is_online: Whether agent is currently reachable
            last_seen: ISO timestamp of last contact
            running_modes: List of modes currently running on agent
            gps_coords: Agent location if available
        """
        modes = {}
        if capabilities and isinstance(capabilities, dict):
            modes = {k: bool(v) for k, v in capabilities.items()}

        devices = []
        ifaces = interfaces or {}
        if isinstance(ifaces, dict):
            devices = ifaces.get('sdr_devices', ifaces.get('devices', []))

        cap = AgentCapability(
            agent_id=agent_id,
            agent_name=agent_name,
            modes=modes,
            devices=devices if isinstance(devices, list) else [],
            interfaces=ifaces,
            is_online=is_online,
            last_seen=last_seen or datetime.now(timezone.utc).isoformat(),
            running_modes=running_modes or [],
            gps_coords=gps_coords,
        )

        with self._lock:
            self._agents[agent_id] = cap
            self._rebuild_fleet_model()

    def remove_agent(self, agent_id: int) -> None:
        """Remove an agent from the aggregator."""
        with self._lock:
            self._agents.pop(agent_id, None)
            self._rebuild_fleet_model()

    def mark_agent_offline(self, agent_id: int) -> None:
        """Mark an agent as offline without removing it."""
        with self._lock:
            if agent_id in self._agents:
                self._agents[agent_id].is_online = False
                self._rebuild_fleet_model()

    def mark_agent_online(self, agent_id: int) -> None:
        """Mark an agent as online."""
        with self._lock:
            if agent_id in self._agents:
                self._agents[agent_id].is_online = True
                self._agents[agent_id].last_seen = datetime.now(timezone.utc).isoformat()
                self._rebuild_fleet_model()

    # =========================================================================
    # Fleet Model
    # =========================================================================

    def _rebuild_fleet_model(self) -> None:
        """
        Rebuild the aggregated fleet capability model from current agent data.

        Must be called with self._lock held.
        """
        # Collect all modes reported by any agent
        mode_agents: dict[str, list[AgentCapability]] = {}
        for cap in self._agents.values():
            for mode_name, enabled in cap.modes.items():
                if enabled:
                    mode_agents.setdefault(mode_name, []).append(cap)

        fleet: list[FleetCapability] = []

        for mode_name, agents in mode_agents.items():
            display = MODE_DISPLAY_MAP.get(mode_name, {
                'label': mode_name.replace('_', ' ').title(),
                'group': 'other',
                'icon': mode_name,
            })

            online_agents = [a for a in agents if a.is_online]
            # An agent is "available" for this mode if it's online and not
            # already running this mode on a conflicting resource.
            available_agents = [
                a for a in online_agents
                if mode_name not in a.running_modes or mode_name in NETWORK_ONLY_MODES
            ]

            if available_agents:
                state = CapabilityState.AVAILABLE
            elif online_agents:
                state = CapabilityState.BUSY
            else:
                state = CapabilityState.OFFLINE

            supporting = [
                {
                    'agent_id': a.agent_id,
                    'agent_name': a.agent_name,
                    'is_online': a.is_online,
                    'is_running': mode_name in a.running_modes,
                }
                for a in agents
            ]

            fleet.append(FleetCapability(
                mode=mode_name,
                label=display['label'],
                group=display['group'],
                icon=display['icon'],
                state=state,
                supporting_agents=supporting,
                available_agent_count=len(available_agents),
                total_agent_count=len(agents),
            ))

        # Sort by group order, then by label
        def sort_key(cap: FleetCapability) -> tuple:
            group_order = GROUP_DISPLAY_MAP.get(cap.group, {}).get('order', 99)
            return (group_order, cap.label)

        fleet.sort(key=sort_key)
        self._fleet_model = fleet
        self._last_rebuild = time.time()

    def get_fleet_capabilities(self) -> list[dict]:
        """
        Get the current fleet capability model.

        Returns:
            List of capability dicts with mode, label, group, state, agents.
        """
        with self._lock:
            return [cap.to_dict() for cap in self._fleet_model]

    def get_fleet_summary(self) -> dict:
        """
        Get a high-level summary of fleet status.

        Returns:
            Dict with counts, agent list, and grouped capabilities.
        """
        with self._lock:
            agents_list = [
                {
                    'agent_id': a.agent_id,
                    'agent_name': a.agent_name,
                    'is_online': a.is_online,
                    'last_seen': a.last_seen,
                    'mode_count': sum(1 for v in a.modes.values() if v),
                    'running_modes': a.running_modes,
                    'gps_coords': a.gps_coords,
                }
                for a in self._agents.values()
            ]

            # Group capabilities
            groups: dict[str, list[dict]] = {}
            for cap in self._fleet_model:
                groups.setdefault(cap.group, []).append(cap.to_dict())

            # Add group metadata
            grouped: list[dict] = []
            for group_key, caps in sorted(
                groups.items(),
                key=lambda x: GROUP_DISPLAY_MAP.get(x[0], {}).get('order', 99)
            ):
                group_meta = GROUP_DISPLAY_MAP.get(group_key, {'label': group_key.title(), 'order': 99})
                grouped.append({
                    'group': group_key,
                    'label': group_meta['label'],
                    'order': group_meta['order'],
                    'capabilities': caps,
                })

            online_count = sum(1 for a in self._agents.values() if a.is_online)

            return {
                'agents': agents_list,
                'agent_count': len(self._agents),
                'online_count': online_count,
                'capability_groups': grouped,
                'total_capabilities': len(self._fleet_model),
                'available_capabilities': sum(
                    1 for c in self._fleet_model if c.state == CapabilityState.AVAILABLE
                ),
                'last_rebuild': self._last_rebuild,
            }

    def get_agents_for_mode(self, mode: str) -> list[dict]:
        """
        Get agents that support a specific mode.

        Args:
            mode: Mode name to look up

        Returns:
            List of agent info dicts, sorted by availability.
        """
        with self._lock:
            result = []
            for agent in self._agents.values():
                if agent.modes.get(mode):
                    result.append({
                        'agent_id': agent.agent_id,
                        'agent_name': agent.agent_name,
                        'is_online': agent.is_online,
                        'is_running': mode in agent.running_modes,
                        'devices': agent.devices,
                        'gps_coords': agent.gps_coords,
                    })
            # Sort: online and not running first
            result.sort(key=lambda a: (not a['is_online'], a['is_running']))
            return result

    def get_capability_state(self, mode: str) -> CapabilityState:
        """
        Get the current state of a specific capability.

        Args:
            mode: Mode name

        Returns:
            CapabilityState enum value
        """
        with self._lock:
            for cap in self._fleet_model:
                if cap.mode == mode:
                    return cap.state
            return CapabilityState.UNSUPPORTED

    def select_agent_for_mode(self, mode: str) -> dict | None:
        """
        Select the best available agent for a given mode.

        Simple first-available strategy. Returns None if no agent is
        available.

        Args:
            mode: Mode name to execute

        Returns:
            Agent info dict or None
        """
        agents = self.get_agents_for_mode(mode)
        for agent in agents:
            if agent['is_online'] and not agent['is_running']:
                return agent
        return None

    # =========================================================================
    # Sync from database
    # =========================================================================

    def sync_from_database(self) -> None:
        """
        Load agent data from the database and rebuild the fleet model.

        This is called on startup and can be called periodically to ensure
        consistency between the database and the in-memory model.
        """
        try:
            from utils.database import list_agents
            agents = list_agents(active_only=False)

            with self._lock:
                self._agents.clear()
                for agent in agents:
                    # Determine online status from last_seen
                    is_online = False
                    if agent.get('last_seen') and agent.get('is_active'):
                        try:
                            last_seen_str = agent['last_seen']
                            if isinstance(last_seen_str, str):
                                # Handle both ISO format and SQLite format
                                if 'T' in last_seen_str:
                                    last_dt = datetime.fromisoformat(last_seen_str.replace('Z', '+00:00'))
                                else:
                                    last_dt = datetime.strptime(last_seen_str, '%Y-%m-%d %H:%M:%S')
                                    last_dt = last_dt.replace(tzinfo=timezone.utc)
                                age = (datetime.now(timezone.utc) - last_dt).total_seconds()
                                is_online = age < self.agent_offline_threshold_seconds
                        except (ValueError, TypeError):
                            pass

                    self._agents[agent['id']] = AgentCapability(
                        agent_id=agent['id'],
                        agent_name=agent['name'],
                        modes=agent.get('capabilities') or {},
                        devices=(agent.get('interfaces') or {}).get('sdr_devices', [])
                            if isinstance(agent.get('interfaces'), dict) else [],
                        interfaces=agent.get('interfaces') or {},
                        is_online=is_online,
                        last_seen=agent.get('last_seen'),
                        gps_coords=agent.get('gps_coords'),
                    )

                self._rebuild_fleet_model()

            logger.info(
                "Synced %d agents from database (%d online)",
                len(self._agents),
                sum(1 for a in self._agents.values() if a.is_online),
            )
        except Exception:
            logger.exception("Failed to sync agents from database")


# Module-level singleton
fleet_aggregator = CapabilityAggregator()
