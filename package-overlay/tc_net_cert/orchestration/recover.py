"""Return NICs to the root namespace after a crash or SIGKILL."""

from __future__ import annotations

from tc_net_cert.config.loader import load_config
from tc_net_cert.core.executor import Executor
from tc_net_cert.core.models import ActiveSession
from tc_net_cert.namespaces.manager import NamespaceManager
from tc_net_cert.network.discovery import discover_interfaces, index_by_mac
from tc_net_cert.network.management import restore_management_routing
from tc_net_cert.storage.runs import session_state_path


def recover(executor: Executor, *, force_prefixes: bool = False) -> list[str]:
    """Restore the active session file. Optionally delete leftover tool namespaces."""
    actions: list[str] = []
    path = session_state_path()
    manager = NamespaceManager(executor)
    if path.exists():
        session = ActiveSession.model_validate_json(path.read_text(encoding="utf-8"))
        moved = {item.logical_name for item in session.snapshots}
        missing = manager.restore_moved(session, moved)
        if missing:
            actions.append("Still missing: " + ", ".join(missing))
        else:
            path.unlink(missing_ok=True)
            actions.append(f"Restored session {session.run_id or path.name}")
    if force_prefixes:
        for name in manager.list_namespaces():
            if name.startswith("ns-lan") or name.startswith("ns-tcnc-"):
                result = executor.run(["ip", "netns", "del", name], timeout=10)
                actions.append(f"deleted {name} (exit {result.exit_code})")
    if not actions:
        actions.append("No active TC-NET-CERT session to restore")
    wifi_iface = ""
    ethernet: list[str] = []
    try:
        config, _path = load_config()
        records = discover_interfaces(executor)
        by_mac = index_by_mac(records)
        for port in config.ports.values():
            record = by_mac.get(port.mac)
            if record is not None:
                ethernet.append(record.name)
        for record in records:
            if record.is_wireless:
                wifi_iface = record.name
                break
    except Exception:
        pass
    restore_management_routing(executor, wifi_iface=wifi_iface, ethernet_ifaces=ethernet)
    if wifi_iface:
        actions.append(f"Default route preference restored via {wifi_iface}")
    return actions
