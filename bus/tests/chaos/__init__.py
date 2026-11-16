"""Deterministic chaos testing harness & oracle for `agent_bus` resilience v2."""

from .hub_proc import (
    BUS_ROOT,
    FORBIDDEN_PORTS,
    HUB_A_PORT,
    HUB_B_PORT,
    HUB_HOST,
    HubController,
    HubInstance,
    assert_ports_free,
    is_port_listening,
    pick_free_port,
    validate_sandbox_port,
    write_claude_guard_script,
)
from .oracle import (
    ChaosOracle,
    ClaudeStub,
    ExecutionGate,
    LedgerEntry,
    RequestLedger,
    SideEffectCounter,
    extract_req_id,
    make_ask_prompt,
)
from .tcp_proxy import PROXY_B_PORT, PROXY_PORT, ProxyController, TcpFaultProxy

__all__ = [
    "BUS_ROOT",
    "FORBIDDEN_PORTS",
    "HUB_A_PORT",
    "HUB_B_PORT",
    "HUB_HOST",
    "PROXY_PORT",
    "PROXY_B_PORT",
    "HubInstance",
    "HubController",
    "TcpFaultProxy",
    "ProxyController",
    "LedgerEntry",
    "RequestLedger",
    "SideEffectCounter",
    "ExecutionGate",
    "ClaudeStub",
    "ChaosOracle",
    "assert_ports_free",
    "is_port_listening",
    "pick_free_port",
    "validate_sandbox_port",
    "write_claude_guard_script",
    "make_ask_prompt",
    "extract_req_id",
]
