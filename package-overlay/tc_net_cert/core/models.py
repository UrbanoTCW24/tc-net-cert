"""Data contracts shared by discovery, tests, and the auditable report."""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field


class Verdict(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"
    WARNING = "WARNING"
    INCONCLUSIVE = "INCONCLUSIVE"
    SKIPPED = "SKIPPED"
    NOT_RUN = "NOT_RUN"


class Bottleneck(str, Enum):
    NONE = "NONE"
    DUT_LIMIT = "DUT LIMIT"
    HOST_LIMIT = "HOST LIMIT"
    NIC_LIMIT = "NIC LIMIT"
    USB_LIMIT = "USB LIMIT"
    LINK_LIMIT = "LINK LIMIT"
    UNKNOWN = "UNKNOWN"


class NetworkMode(str, Enum):
    DHCP = "DHCP"
    STATIC = "STATIC ENGINEERING"


VERDICT_RANK = {
    Verdict.PASS: 0,
    Verdict.SKIPPED: 1,
    Verdict.NOT_RUN: 1,
    Verdict.WARNING: 2,
    Verdict.INCONCLUSIVE: 3,
    Verdict.FAIL: 4,
}


def worst_verdict(values: list[Verdict]) -> Verdict:
    considered = [item for item in values if item not in {Verdict.SKIPPED, Verdict.NOT_RUN}]
    if not considered:
        return Verdict.SKIPPED
    return max(considered, key=lambda item: VERDICT_RANK[item])


class BenchDefinition(BaseModel):
    id: str = ""
    label: str = ""
    ports: list[str] = Field(default_factory=list)


class PortConfig(BaseModel):
    mac: str


class AcceptanceProfile(BaseModel):
    expected_link_speed: int
    expected_duplex: str = "full"
    minimum_throughput_mbps: float
    max_rx_errors: int = 0
    max_tx_errors: int = 0
    max_drops: int = 0
    max_carrier_changes: int = 0
    link_flap_result: str = "FAIL"
    max_ping_loss_percent: float = 0
    cpu_busy_percent: float = 90
    require_autoneg: bool = False


class SpeedProfile(BaseModel):
    expected_speed_mbps: int
    minimum_throughput_mbps: float


class DutProfile(BaseModel):
    mode: str
    ports: int
    test_cross_ports: bool = False
    test_gateway: bool = True
    simultaneous: bool = False
    bidirectional: bool = False
    throughput: bool = True


class DhcpSettings(BaseModel):
    backend: str = "internal"
    timeout_seconds: int = 30


class IperfSettings(BaseModel):
    duration_seconds: int = 120
    streams: int = 4
    base_port: int = 5201
    run_single_stream: bool = False
    allowed_durations: list[int] = Field(default_factory=lambda: [30, 60, 120, 300, 600, 1800, 3600, 7200])
    allowed_streams: list[int] = Field(default_factory=lambda: [1, 2, 4, 8])


class PingSettings(BaseModel):
    count: int = 5
    timeout_seconds: int = 2


class WanService(BaseModel):
    """One provisioned WAN the ONT portal lists. No credentials."""

    name: str = ""
    service: str = ""
    status: str = ""
    mode: str = ""
    vlan: str = ""
    address: str = ""
    gateway: str = ""
    nat: str = ""
    uptime: str = ""


class RfChannel(BaseModel):
    """One downstream or upstream row read from the modem portal."""

    channel: str = ""
    state: str = ""
    modulation: str = ""
    channel_id: str = ""
    frequency: str = ""
    power: str = ""
    snr: str = ""
    correctable: str = ""
    uncorrectable: str = ""
    symbol_rate: str = ""
    bandwidth: str = ""


class DutIdentity(BaseModel):
    type: str = "ont"
    brand: str = ""
    model: str = ""
    serial: str = ""
    mac: str = ""
    firmware: str = ""
    hardware: str = ""
    device_type: str = ""
    optical_tx: str = ""
    optical_rx: str = ""
    optical_voltage: str = ""
    optical_bias: str = ""
    optical_temperature: str = ""
    ont_state: str = ""
    ont_id: str = ""
    pon_mode: str = ""
    optic_link: str = ""
    los_status: str = ""
    tx_wavelength: str = ""
    rx_wavelength: str = ""
    optic_vendor: str = ""
    optic_module: str = ""
    optic_distance: str = ""
    vendor_id: str = ""
    release_time: str = ""
    description: str = ""
    registration: str = ""
    olt_service: str = ""
    ems_status: str = ""
    acs_status: str = ""
    wan_rows: list[WanService] = Field(default_factory=list)
    wlan_status: str = ""
    wlan_channel: str = ""
    ssid_24: str = ""
    ssid_50: str = ""
    wifi_password_24: str = ""
    wifi_password_50: str = ""
    docsis_status: str = ""
    ds_channels: str = ""
    ds_power: str = ""
    ds_correctable_total: str = ""
    ds_uncorrectable_total: str = ""
    us_channels: str = ""
    us_power: str = ""
    mer: str = ""
    uptime: str = ""
    cm_ip: str = ""
    mta_dhcp: str = ""
    wan_dhcp: str = ""
    ds_uncorrectable_delta: str = ""
    ds_rows: list[RfChannel] = Field(default_factory=list)
    us_rows: list[RfChannel] = Field(default_factory=list)
    lot: str = ""
    work_order: str = ""


class AppConfig(BaseModel):
    schema_version: str
    safe_mode: bool = True
    operator: str = ""
    station: str = ""
    runs_dir: str = ""
    report_title: str
    active_profile: str
    active_speed_profile: str
    ports: dict[str, PortConfig] = Field(default_factory=dict)
    network_mode: str = "dhcp"
    management_mac: str = ""
    static_addresses: dict[str, str] = Field(default_factory=dict)
    static_gateways: dict[str, str] = Field(default_factory=dict)
    simultaneous_pairs: list[list[str]] = Field(default_factory=list)
    speed_profiles: dict[str, SpeedProfile]
    profiles: dict[str, AcceptanceProfile]
    dut_profiles: dict[str, DutProfile]
    dhcp: DhcpSettings = Field(default_factory=DhcpSettings)
    iperf: IperfSettings = Field(default_factory=IperfSettings)
    ping: PingSettings = Field(default_factory=PingSettings)
    dut: DutIdentity = Field(default_factory=DutIdentity)
    benches: dict[str, BenchDefinition] = Field(default_factory=dict)
    long_duration_seconds: int = 300
    require_profile_port_count: bool = True

    def acceptance(self) -> AcceptanceProfile:
        return self.profiles[self.active_profile]

    def dut_profile(self) -> DutProfile:
        return self.dut_profiles[self.dut.type]


class InterfaceRecord(BaseModel):
    name: str
    mac: str
    operstate: str = ""
    link_type: str = ""
    driver: str = ""
    vendor: str = ""
    model: str = ""
    addresses: list[str] = Field(default_factory=list)
    is_wireless: bool = False
    master: str = ""
    excluded: bool = False
    exclusion_reason: str = ""
    protected: bool = False
    protection_reasons: list[str] = Field(default_factory=list)

    @property
    def display_vendor(self) -> str:
        text = " ".join(part for part in (self.vendor, self.model) if part).strip()
        return text or self.driver or "unknown"


class ManagementPlane(BaseModel):
    default_route_interface: str = ""
    default_route_gateway: str = ""
    ssh_server_ip: str = ""
    ssh_interface: str = ""
    protected_interfaces: list[str] = Field(default_factory=list)
    reasons: dict[str, list[str]] = Field(default_factory=dict)


class UsbInfo(BaseModel):
    is_usb: bool = False
    vendor_id: str = ""
    product_id: str = ""
    manufacturer: str = ""
    product: str = ""
    bus: str = ""
    usb_speed_mbps: int | None = None
    usb_generation: str = ""
    driver: str = ""
    note: str = ""


class LinkFacts(BaseModel):
    detected: bool = False
    speed_mbps: int | None = None
    duplex: str = ""
    autoneg: str = ""
    driver: str = ""
    firmware: str = ""
    bus_info: str = ""
    rx_packets: int = 0
    tx_packets: int = 0
    rx_errors: int = 0
    tx_errors: int = 0
    rx_dropped: int = 0
    tx_dropped: int = 0
    collisions: int = 0
    carrier_changes: int = 0
    operstate: str = ""
    supported_speeds_mbps: list[int] = Field(default_factory=list)


class ErrorDelta(BaseModel):
    rx_errors_before: int = 0
    rx_errors_after: int = 0
    delta_rx_errors: int = 0
    tx_errors_before: int = 0
    tx_errors_after: int = 0
    delta_tx_errors: int = 0
    rx_dropped_before: int = 0
    rx_dropped_after: int = 0
    delta_rx_dropped: int = 0
    tx_dropped_before: int = 0
    tx_dropped_after: int = 0
    delta_tx_dropped: int = 0
    carrier_changes_before: int = 0
    carrier_changes_after: int = 0
    delta_carrier_changes: int = 0
    link_flaps: int = 0
    verdict: Verdict = Verdict.NOT_RUN
    reasons: list[str] = Field(default_factory=list)


class DhcpFacts(BaseModel):
    success: bool = False
    ip: str = ""
    prefixlen: int | None = None
    mask: str = ""
    gateway: str = ""
    dns: list[str] = Field(default_factory=list)
    lease_seconds: int | None = None
    dhcp_server: str = ""
    acquisition_seconds: float | None = None
    backend: str = ""
    message: str = ""
    verdict: Verdict = Verdict.NOT_RUN
    reasons: list[str] = Field(default_factory=list)


class PingFacts(BaseModel):
    target: str = ""
    transmitted: int = 0
    received: int = 0
    loss_percent: float | None = None
    rtt_min_ms: float | None = None
    rtt_avg_ms: float | None = None
    rtt_max_ms: float | None = None
    rtt_mdev_ms: float | None = None
    verdict: Verdict = Verdict.NOT_RUN
    reasons: list[str] = Field(default_factory=list)


class HostSample(BaseModel):
    cpu_percent_max: float | None = None
    cpu_percent_avg: float | None = None
    memory_percent: float | None = None
    load_average: list[float] = Field(default_factory=list)
    temperature_c: float | None = None
    iperf_cpu_host: float | None = None


class FlowResult(BaseModel):
    case_id: str
    kind: str
    source: str
    destination: str
    direction: str = "forward"
    streams: int = 1
    requested_duration_s: float = 0
    actual_duration_s: float | None = None
    long_duration: bool = False
    mbps_avg: float | None = None
    mbps_min: float | None = None
    mbps_max: float | None = None
    retransmits: int | None = None
    max_snd_cwnd: int | None = None
    ping: PingFacts | None = None
    error_delta: ErrorDelta | None = None
    host: HostSample | None = None
    bottleneck: Bottleneck = Bottleneck.NONE
    assessment: str = ""
    verdict: Verdict = Verdict.NOT_RUN
    reasons: list[str] = Field(default_factory=list)
    raw_json: str = ""
    source_ip: str = ""
    destination_ip: str = ""


class MatrixCell(BaseModel):
    mbps: float | None = None
    verdict: Verdict = Verdict.NOT_RUN
    retransmits: int | None = None
    note: str = ""


class PortReport(BaseModel):
    logical_name: str
    interface: str
    mac: str
    namespace: str
    vendor: str = ""
    driver: str = ""
    firmware: str = ""
    usb: UsbInfo | None = None
    link: LinkFacts = Field(default_factory=LinkFacts)
    physical_verdict: Verdict = Verdict.NOT_RUN
    physical_reasons: list[str] = Field(default_factory=list)
    dhcp: DhcpFacts | None = None
    address: str = ""
    gateway: PingFacts | None = None
    cross_verdict: Verdict = Verdict.NOT_RUN
    throughput_avg_mbps: float | None = None
    throughput_verdict: Verdict = Verdict.NOT_RUN
    tx_mbps: float | None = None
    rx_mbps: float | None = None
    iperf_state: Verdict = Verdict.NOT_RUN
    iperf_duration_s: float | None = None
    failure_reason: str = ""
    rx_errors: int = 0
    tx_errors: int = 0
    drops: int = 0
    link_flaps: int = 0
    verdict: Verdict = Verdict.NOT_RUN
    reasons: list[str] = Field(default_factory=list)


class CommandLogEntry(BaseModel):
    timestamp: str
    namespace: str = ""
    interface: str = ""
    command: str
    exit_code: int | None = None
    timed_out: bool = False
    duration_s: float = 0
    stdout_file: str = ""
    stderr_file: str = ""


class HostInfo(BaseModel):
    hostname: str = ""
    os: str = ""
    kernel: str = ""
    cpu: str = ""
    memory_mb: int | None = None
    python: str = ""
    tool_version: str = ""


class StationCleanup(BaseModel):
    verdict: Verdict = Verdict.NOT_RUN
    namespaces_after: list[str] = Field(default_factory=list)
    reasons: list[str] = Field(default_factory=list)


class CertificationReport(BaseModel):
    title: str
    run_id: str
    tool_version: str
    schema_version: str
    config_hash: str
    network_mode: NetworkMode
    profile: str
    speed_profile: str
    dut_profile: str
    dut: DutIdentity
    host: HostInfo
    operator: str = ""
    station: str = ""
    started_at_utc: str
    started_at_local: str
    finished_at_utc: str = ""
    finished_at_local: str = ""
    ports: list[PortReport] = Field(default_factory=list)
    flows: list[FlowResult] = Field(default_factory=list)
    matrix: dict[str, dict[str, MatrixCell]] = Field(default_factory=dict)
    simultaneous_verdict: Verdict = Verdict.NOT_RUN
    bidirectional_verdict: Verdict = Verdict.NOT_RUN
    errors_verdict: Verdict = Verdict.NOT_RUN
    stability_verdict: Verdict = Verdict.NOT_RUN
    global_verdict: Verdict = Verdict.NOT_RUN
    global_reasons: list[str] = Field(default_factory=list)
    scope_exclusions: list[str] = Field(default_factory=list)
    disclaimer: str = ""
    cleanup: StationCleanup = Field(default_factory=StationCleanup)
    stages: list[str] = Field(default_factory=list)
    wifi_interface: str = ""
    wifi_address: str = ""
    wifi_state: str = ""
    wifi_excluded: bool = True
    job: str = ""
    dut_identity_note: str = ""
    bench_scope: str = ""
    bench_duts: dict[str, DutIdentity] = Field(default_factory=dict)
    bench_orders: dict[str, str] = Field(default_factory=dict)
    dual_session_run: str = ""


class IfaceSnapshot(BaseModel):
    name: str
    mac: str
    operstate: str = ""
    addresses: list[str] = Field(default_factory=list)
    mtu: int | None = None
    namespace: str
    logical_name: str


class ActiveSession(BaseModel):
    run_id: str = ""
    snapshots: list[IfaceSnapshot] = Field(default_factory=list)
    namespaces: list[str] = Field(default_factory=list)
    started_at_utc: str = ""


class Endpoint(BaseModel):
    logical_name: str
    interface: str
    mac: str
    namespace: str
    address: str = ""
    prefixlen: int | None = None
    gateway: str = ""
    iperf_port: int = 5201
    usb: UsbInfo | None = None
    identity_vendor: str = ""
    driver: str = ""
    firmware: str = ""
    link: LinkFacts = Field(default_factory=LinkFacts)
    dhcp: DhcpFacts | None = None
    supported_speeds_mbps: list[int] = Field(default_factory=list)
    physical_verdict: Verdict = Verdict.NOT_RUN
    physical_reasons: list[str] = Field(default_factory=list)
    gateway_result: PingFacts | None = None
    stage: str = "IDLE"
    iperf_state: Verdict = Verdict.NOT_RUN
    failure_reason: str = ""
    tx_mbps: float | None = None
    rx_mbps: float | None = None
    iperf_duration_s: float | None = None


class PreflightCheck(BaseModel):
    name: str
    verdict: Verdict
    detail: str
