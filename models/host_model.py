"""
Modelo de Host — v2.3 com histórico triplo (HOST SSH + WAN + Google).
Cada tipo tem deque para disponibilidade. HOST SSH faz fallback para local.
"""
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional
from collections import deque

from config import MONITOR_DEFAULTS


@dataclass
class PingResult:
    timestamp: datetime
    latency_ms: float = 0.0
    jitter_ms: float = 0.0
    loss_pct: float = 0.0
    rtt_min: float = 0.0
    rtt_max: float = 0.0
    rtt_avg: float = 0.0
    ttl: int = 0
    status: str = "unknown"
    packets_sent: int = 4
    packets_recv: int = 0
    ping_mode: str = "PING"
    gateway: str = ""

    @property
    def is_online(self) -> bool:
        return self.status == "online"


@dataclass
class TracerouteHop:
    hop_number: int
    ip: str = "*"
    hostname: str = ""
    rtt1: float = 0.0
    rtt2: float = 0.0
    rtt3: float = 0.0

    @property
    def avg_rtt(self) -> float:
        rtts = [r for r in [self.rtt1, self.rtt2, self.rtt3] if r > 0]
        return sum(rtts) / len(rtts) if rtts else 0.0


@dataclass
class TracerouteResult:
    timestamp: datetime
    hops: list[TracerouteHop] = field(default_factory=list)
    target_reached: bool = False

    @property
    def hop_count(self) -> int:
        return len(self.hops)


def _avail(dq: deque) -> float:
    if not dq:
        return 0.0
    return (sum(1 for p in dq if p.loss_pct < 100) / len(dq)) * 100


@dataclass
class Host:
    id: int = -1
    ip: str = ""
    label: str = ""
    group_name: str = "Geral"
    ssh_user: str = ""
    ssh_password: str = ""
    ssh_port: int = 22
    wan_ip: str = ""
    wan_ip_2: str = ""         # WAN secundária (gateway ifconfig.me hop-2)
    wan_ip_3: str = ""         # WAN terciária (primeiro IP público pós-gateway)
    platform: str = ""         # Plataforma do roteador (pfsense, mikrotik, cisco, etc.)
    cisco_enable_password: str = ""  # Senha enable do Cisco (diferente da SSH)
    last_ping_mode: str = "PING"
    enabled: bool = True

    status: str = "unknown"
    last_seen: Optional[datetime] = None
    consecutive_failures: int = 0
    last_alert_time: Optional[datetime] = None
    offline_since: Optional[datetime] = None
    alerts_fired: int = 0
    cycle_number: int = 0
    ping_in_cycle: int = 0
    total_pings_all_time: int = 0
    last_collection_ts: Optional[datetime] = None

    # Históricos
    ping_history: deque = field(default_factory=lambda: deque(maxlen=500))
    host_ssh_history: deque = field(default_factory=lambda: deque(maxlen=200))
    wan_history: deque = field(default_factory=lambda: deque(maxlen=200))
    google_history: deque = field(default_factory=lambda: deque(maxlen=200))
    latest_traceroute: Optional[TracerouteResult] = None

    # Últimos resultados SSH
    host_ssh_ping_last: Optional[PingResult] = None
    wan_ping_last: Optional[PingResult] = None
    google_ping_last: Optional[PingResult] = None

    @property
    def display_name(self) -> str:
        return self.label if self.label else self.ip

    @property
    def is_online(self) -> bool:
        return self.status == "online"

    # ── HOST IP (SSH com fallback local) ──────────────────────────────
    @property
    def host_ssh_latency(self) -> float:
        if self.host_ssh_ping_last:
            return self.host_ssh_ping_last.latency_ms
        return self.current_latency

    @property
    def host_ssh_jitter(self) -> float:
        if self.host_ssh_ping_last:
            return self.host_ssh_ping_last.jitter_ms
        return self.current_jitter

    @property
    def host_ssh_loss(self) -> float:
        if self.host_ssh_ping_last:
            return self.host_ssh_ping_last.loss_pct
        return self.current_loss

    @property
    def host_ssh_rtt(self) -> str:
        if self.host_ssh_ping_last:
            p = self.host_ssh_ping_last
            return f"{p.rtt_min:.0f}/{p.rtt_max:.0f}"
        return self.current_rtt

    @property
    def host_ssh_avail(self) -> float:
        if self.host_ssh_history:
            return _avail(self.host_ssh_history)
        return self.availability_recent

    @property
    def host_ssh_has_data(self) -> bool:
        return self.host_ssh_ping_last is not None or bool(self.ping_history)

    @property
    def host_ssh_source(self) -> str:
        return "SSH" if self.host_ssh_ping_last is not None else "LOCAL"

    # ── WAN IP (SSH, sem fallback) ────────────────────────────────────
    @property
    def wan_latency(self) -> float:
        return self.wan_ping_last.latency_ms if self.wan_ping_last else 0.0

    @property
    def wan_jitter(self) -> float:
        return self.wan_ping_last.jitter_ms if self.wan_ping_last else 0.0

    @property
    def wan_loss(self) -> float:
        return self.wan_ping_last.loss_pct if self.wan_ping_last else 0.0

    @property
    def wan_rtt(self) -> str:
        if self.wan_ping_last:
            p = self.wan_ping_last
            return f"{p.rtt_min:.0f}/{p.rtt_max:.0f}"
        return "—"

    @property
    def wan_avail(self) -> float:
        return _avail(self.wan_history)

    @property
    def wan_has_data(self) -> bool:
        return self.wan_ping_last is not None

    # ── Google (SSH, sem fallback) ────────────────────────────────────
    @property
    def google_latency(self) -> float:
        return self.google_ping_last.latency_ms if self.google_ping_last else 0.0

    @property
    def google_jitter(self) -> float:
        return self.google_ping_last.jitter_ms if self.google_ping_last else 0.0

    @property
    def google_loss(self) -> float:
        return self.google_ping_last.loss_pct if self.google_ping_last else 0.0

    @property
    def google_rtt(self) -> str:
        if self.google_ping_last:
            p = self.google_ping_last
            return f"{p.rtt_min:.0f}/{p.rtt_max:.0f}"
        return "—"

    @property
    def google_avail(self) -> float:
        return _avail(self.google_history)

    @property
    def google_has_data(self) -> bool:
        return self.google_ping_last is not None

    # ── Deltas ────────────────────────────────────────────────────────
    @property
    def delta_wan(self) -> Optional[float]:
        if self.wan_has_data and self.host_ssh_latency > 0:
            return self.wan_latency - self.host_ssh_latency
        return None

    @property
    def delta_google(self) -> Optional[float]:
        if self.google_has_data and self.host_ssh_latency > 0:
            return self.google_latency - self.host_ssh_latency
        return None

    # ── Ping local (sempre disponível) ────────────────────────────────
    @property
    def current_latency(self) -> float:
        return self.ping_history[-1].latency_ms if self.ping_history else 0.0

    @property
    def current_jitter(self) -> float:
        return self.ping_history[-1].jitter_ms if self.ping_history else 0.0

    @property
    def current_loss(self) -> float:
        return self.ping_history[-1].loss_pct if self.ping_history else 0.0

    @property
    def current_rtt(self) -> str:
        if self.ping_history:
            p = self.ping_history[-1]
            return f"{p.rtt_min:.0f}/{p.rtt_max:.0f}"
        return "—"

    @property
    def availability_recent(self) -> float:
        return _avail(self.ping_history)

    @property
    def avg_latency_recent(self) -> float:
        recent = list(self.ping_history)[-20:]
        vals = [p.latency_ms for p in recent if p.latency_ms > 0]
        return sum(vals) / len(vals) if vals else 0.0

    @property
    def success_rate_recent(self) -> float:
        recent = list(self.ping_history)[-100:]
        if not recent:
            return 0.0
        return (sum(1 for p in recent if p.loss_pct < 100) / len(recent)) * 100

    @property
    def stddev_latency_recent(self) -> float:
        import statistics
        vals = [p.latency_ms for p in list(self.ping_history)[-20:] if p.latency_ms > 0]
        return round(statistics.stdev(vals), 2) if len(vals) >= 2 else 0.0

    # ── Mutação ───────────────────────────────────────────────────────
    def add_ping(self, result: PingResult):
        self.ping_history.append(result)
        self.last_collection_ts = result.timestamp
        cycle_size = MONITOR_DEFAULTS.get("cycle_size", 100)
        self.total_pings_all_time += 1
        self.ping_in_cycle += 1
        if self.ping_in_cycle > cycle_size:
            self.ping_in_cycle = 1
            self.cycle_number += 1
        if result.is_online:
            self.status = "online"
            self.last_seen = result.timestamp
            self.consecutive_failures = 0
            self.offline_since = None
            self.alerts_fired = 0
        else:
            self.status = "offline"
            self.consecutive_failures += 1
            if self.offline_since is None:
                self.offline_since = result.timestamp

    def add_ssh_results(self, host_ssh, wan, google):
        self.host_ssh_ping_last = host_ssh
        self.wan_ping_last = wan
        self.google_ping_last = google
        if host_ssh is not None:
            self.host_ssh_history.append(host_ssh)
        if wan is not None:
            self.wan_history.append(wan)
        if google is not None:
            self.google_history.append(google)
        if any(r and r.ping_mode == "SSH" for r in [host_ssh, wan, google]):
            self.last_ping_mode = "SSH"

    _ALERT_SCHEDULE = [0, 300, 600, 900, 1800, 3600]

    def next_alert_at_seconds(self) -> int:
        if self.alerts_fired < len(self._ALERT_SCHEDULE):
            return self._ALERT_SCHEDULE[self.alerts_fired]
        extra = self.alerts_fired - len(self._ALERT_SCHEDULE) + 1
        return self._ALERT_SCHEDULE[-1] + extra * 3600

    def get_retry_interval(self) -> int:
        intervals = [300, 900, 1800, 3600]
        idx = min(self.consecutive_failures - 1, len(intervals) - 1)
        return intervals[max(0, idx)]

    def to_dict(self) -> dict:
        return {
            "id": self.id, "ip": self.ip, "label": self.label,
            "group_name": self.group_name, "ssh_user": self.ssh_user,
            "ssh_password": self.ssh_password, "ssh_port": self.ssh_port,
            "wan_ip": self.wan_ip, "wan_ip_2": self.wan_ip_2,
            "wan_ip_3": self.wan_ip_3, "platform": self.platform,
            "cisco_enable_password": self.cisco_enable_password,
            "enabled": self.enabled,
        }