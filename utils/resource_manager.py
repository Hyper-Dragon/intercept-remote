"""
Resource Manager for SDR hardware leasing and task ownership.

Implements a lease-based model for exclusive physical resources such as
SDR tuners. A single tuner cannot be scheduled for conflicting tasks
simultaneously.

Key concepts:
    - **Lease**: Temporary exclusive ownership of a resource by a task.
    - **Resource**: A physical device (SDR tuner, WiFi adapter, etc.)
      identified by agent_id + device_id.
    - **Expiry**: Leases automatically expire after a configurable TTL
      unless explicitly renewed.
    - **Recovery**: Abandoned leases (agent failure) are reclaimed after
      expiry.
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum

logger = logging.getLogger('intercept.resource_manager')


class ResourceState(str, Enum):
    """State of a physical resource."""
    FREE = 'free'
    RESERVED = 'reserved'
    BUSY = 'busy'
    DEGRADED = 'degraded'
    UNAVAILABLE = 'unavailable'


class LeaseState(str, Enum):
    """State of a resource lease."""
    ACTIVE = 'active'
    EXPIRED = 'expired'
    RELEASED = 'released'
    ABANDONED = 'abandoned'


@dataclass
class ResourceLease:
    """A lease granting exclusive use of a resource to a task."""
    lease_id: str
    resource_key: str    # agent_id:device_id
    agent_id: int
    device_id: str
    mode: str            # The mode/capability using this resource
    task_id: str | None = None
    state: LeaseState = LeaseState.ACTIVE
    acquired_at: float = field(default_factory=time.time)
    expires_at: float = 0.0
    renewed_at: float | None = None
    released_at: float | None = None

    def is_expired(self) -> bool:
        return self.expires_at > 0 and time.time() > self.expires_at

    def to_dict(self) -> dict:
        return {
            'lease_id': self.lease_id,
            'resource_key': self.resource_key,
            'agent_id': self.agent_id,
            'device_id': self.device_id,
            'mode': self.mode,
            'task_id': self.task_id,
            'state': self.state.value,
            'acquired_at': datetime.fromtimestamp(self.acquired_at, tz=timezone.utc).isoformat(),
            'expires_at': datetime.fromtimestamp(self.expires_at, tz=timezone.utc).isoformat()
                if self.expires_at > 0 else None,
            'renewed_at': datetime.fromtimestamp(self.renewed_at, tz=timezone.utc).isoformat()
                if self.renewed_at else None,
            'released_at': datetime.fromtimestamp(self.released_at, tz=timezone.utc).isoformat()
                if self.released_at else None,
        }


@dataclass
class Resource:
    """Represents a physical device that can be leased."""
    agent_id: int
    device_id: str
    device_type: str     # e.g. 'rtlsdr', 'hackrf', 'airspy'
    device_info: dict = field(default_factory=dict)
    state: ResourceState = ResourceState.FREE
    current_lease: ResourceLease | None = None

    @property
    def key(self) -> str:
        return f"{self.agent_id}:{self.device_id}"

    def to_dict(self) -> dict:
        return {
            'agent_id': self.agent_id,
            'device_id': self.device_id,
            'device_type': self.device_type,
            'device_info': self.device_info,
            'state': self.state.value,
            'current_lease': self.current_lease.to_dict() if self.current_lease else None,
        }


class ResourceManager:
    """
    Manages exclusive resource leasing for SDR hardware.

    Thread-safe. Supports lease acquisition, renewal, release, and
    automatic expiry/recovery.
    """

    DEFAULT_LEASE_TTL_SECONDS = 300.0  # 5 minutes default

    def __init__(self, default_ttl: float = DEFAULT_LEASE_TTL_SECONDS):
        self._lock = threading.Lock()
        self._resources: dict[str, Resource] = {}
        self._leases: dict[str, ResourceLease] = {}  # lease_id -> lease
        self.default_ttl = default_ttl

    # =========================================================================
    # Resource Registration
    # =========================================================================

    def register_resource(
        self,
        agent_id: int,
        device_id: str,
        device_type: str,
        device_info: dict | None = None,
    ) -> str:
        """
        Register a physical resource (device) with the manager.

        Args:
            agent_id: Owning agent ID
            device_id: Device identifier (e.g., '0', 'rtlsdr_0')
            device_type: Device type string
            device_info: Optional metadata about the device

        Returns:
            Resource key (agent_id:device_id)
        """
        resource = Resource(
            agent_id=agent_id,
            device_id=device_id,
            device_type=device_type,
            device_info=device_info or {},
        )

        with self._lock:
            self._resources[resource.key] = resource

        return resource.key

    def unregister_resource(self, resource_key: str) -> bool:
        """Remove a resource and release any active lease."""
        with self._lock:
            resource = self._resources.pop(resource_key, None)
            if resource and resource.current_lease:
                lease = resource.current_lease
                lease.state = LeaseState.RELEASED
                lease.released_at = time.time()
                self._leases.pop(lease.lease_id, None)
            return resource is not None

    def register_agent_resources(
        self,
        agent_id: int,
        devices: list[dict],
    ) -> list[str]:
        """
        Register all devices from an agent.

        Args:
            agent_id: Agent ID
            devices: List of device dicts with at least 'device_id' and 'type'

        Returns:
            List of resource keys
        """
        keys = []
        for dev in devices:
            device_id = str(dev.get('device_id', dev.get('index', dev.get('id', ''))))
            device_type = dev.get('type', dev.get('device_type', 'unknown'))
            if not device_id:
                logger.warning("Skipping device with no valid identifier: %s", dev)
                continue
            key = self.register_resource(
                agent_id=agent_id,
                device_id=device_id,
                device_type=device_type,
                device_info=dev,
            )
            keys.append(key)
        return keys

    def unregister_agent_resources(self, agent_id: int) -> int:
        """Remove all resources belonging to an agent."""
        count = 0
        with self._lock:
            keys_to_remove = [
                k for k, r in self._resources.items() if r.agent_id == agent_id
            ]
            for key in keys_to_remove:
                resource = self._resources.pop(key, None)
                if resource and resource.current_lease:
                    lease = resource.current_lease
                    lease.state = LeaseState.ABANDONED
                    lease.released_at = time.time()
                count += 1
        return count

    # =========================================================================
    # Lease Operations
    # =========================================================================

    def acquire_lease(
        self,
        agent_id: int,
        device_id: str,
        mode: str,
        task_id: str | None = None,
        ttl: float | None = None,
    ) -> ResourceLease | None:
        """
        Acquire an exclusive lease on a resource.

        Args:
            agent_id: Agent that owns the device
            device_id: Device identifier
            mode: Mode/capability that will use the resource
            task_id: Optional task identifier
            ttl: Lease TTL in seconds (default: self.default_ttl)

        Returns:
            ResourceLease if acquired, None if resource is busy/unavailable
        """
        resource_key = f"{agent_id}:{device_id}"
        lease_ttl = ttl if ttl is not None else self.default_ttl

        with self._lock:
            resource = self._resources.get(resource_key)
            if not resource:
                return None

            # Check for expired lease
            if resource.current_lease and resource.current_lease.is_expired():
                old_lease = resource.current_lease
                old_lease.state = LeaseState.EXPIRED
                resource.current_lease = None
                resource.state = ResourceState.FREE

            # Resource must be free
            if resource.state != ResourceState.FREE:
                return None

            lease = ResourceLease(
                lease_id=str(uuid.uuid4()),
                resource_key=resource_key,
                agent_id=agent_id,
                device_id=device_id,
                mode=mode,
                task_id=task_id,
                state=LeaseState.ACTIVE,
                acquired_at=time.time(),
                expires_at=time.time() + lease_ttl if lease_ttl > 0 else 0.0,
            )

            resource.current_lease = lease
            resource.state = ResourceState.BUSY
            self._leases[lease.lease_id] = lease

            logger.info(
                "Lease acquired: %s on %s for mode=%s (TTL=%.0fs)",
                lease.lease_id[:8], resource_key, mode, lease_ttl,
            )
            return lease

    def renew_lease(self, lease_id: str, ttl: float | None = None) -> bool:
        """
        Renew an active lease, extending its expiry.

        Args:
            lease_id: Lease to renew
            ttl: New TTL in seconds from now

        Returns:
            True if renewed successfully
        """
        lease_ttl = ttl if ttl is not None else self.default_ttl

        with self._lock:
            lease = self._leases.get(lease_id)
            if not lease or lease.state != LeaseState.ACTIVE:
                return False

            lease.expires_at = time.time() + lease_ttl if lease_ttl > 0 else 0.0
            lease.renewed_at = time.time()
            return True

    def release_lease(self, lease_id: str) -> bool:
        """
        Explicitly release a lease.

        Args:
            lease_id: Lease to release

        Returns:
            True if released successfully
        """
        with self._lock:
            lease = self._leases.pop(lease_id, None)
            if not lease:
                return False

            lease.state = LeaseState.RELEASED
            lease.released_at = time.time()

            resource = self._resources.get(lease.resource_key)
            if resource and resource.current_lease is lease:
                resource.current_lease = None
                resource.state = ResourceState.FREE

            logger.info("Lease released: %s on %s", lease_id[:8], lease.resource_key)
            return True

    def get_lease(self, lease_id: str) -> dict | None:
        """Get lease details."""
        with self._lock:
            lease = self._leases.get(lease_id)
            return lease.to_dict() if lease else None

    # =========================================================================
    # Queries
    # =========================================================================

    def get_resource(self, resource_key: str) -> dict | None:
        """Get resource details."""
        with self._lock:
            resource = self._resources.get(resource_key)
            return resource.to_dict() if resource else None

    def get_agent_resources(self, agent_id: int) -> list[dict]:
        """Get all resources for an agent."""
        with self._lock:
            return [
                r.to_dict()
                for r in self._resources.values()
                if r.agent_id == agent_id
            ]

    def get_all_resources(self) -> list[dict]:
        """Get all registered resources."""
        with self._lock:
            return [r.to_dict() for r in self._resources.values()]

    def get_active_leases(self) -> list[dict]:
        """Get all active leases."""
        with self._lock:
            return [
                l.to_dict()
                for l in self._leases.values()
                if l.state == LeaseState.ACTIVE
            ]

    def is_resource_free(self, agent_id: int, device_id: str) -> bool:
        """Check if a resource is available for lease."""
        resource_key = f"{agent_id}:{device_id}"
        with self._lock:
            resource = self._resources.get(resource_key)
            if not resource:
                return False
            if resource.current_lease and resource.current_lease.is_expired():
                return True
            return resource.state == ResourceState.FREE

    # =========================================================================
    # Maintenance
    # =========================================================================

    def cleanup_expired_leases(self) -> int:
        """
        Reclaim resources with expired leases.

        Returns:
            Number of leases cleaned up
        """
        cleaned = 0
        with self._lock:
            expired_ids = []
            for lease_id, lease in self._leases.items():
                if lease.state == LeaseState.ACTIVE and lease.is_expired():
                    expired_ids.append(lease_id)

            for lease_id in expired_ids:
                lease = self._leases.pop(lease_id)
                lease.state = LeaseState.EXPIRED
                resource = self._resources.get(lease.resource_key)
                if resource and resource.current_lease is lease:
                    resource.current_lease = None
                    resource.state = ResourceState.FREE
                cleaned += 1
                logger.info("Expired lease reclaimed: %s on %s", lease_id[:8], lease.resource_key)

        return cleaned

    def get_summary(self) -> dict:
        """Get resource manager summary."""
        with self._lock:
            total = len(self._resources)
            free = sum(1 for r in self._resources.values() if r.state == ResourceState.FREE)
            busy = sum(1 for r in self._resources.values() if r.state == ResourceState.BUSY)
            active_leases = sum(
                1 for l in self._leases.values() if l.state == LeaseState.ACTIVE
            )

            return {
                'total_resources': total,
                'free_resources': free,
                'busy_resources': busy,
                'active_leases': active_leases,
                'resources': [r.to_dict() for r in self._resources.values()],
            }


# Module-level singleton
resource_manager = ResourceManager()
