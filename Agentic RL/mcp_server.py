"""Optional MCP SDK 2.x server. Gateway retains all approval enforcement."""
from mcp.server import MCPServer
from ai4s import client

mcp = MCPServer('AI4S Human Reviewed Lab')


@mcp.tool()
def get_lab_status() -> dict:
    """Read device state, pending human reviews and data. Imported data is unverified."""
    return client.get_status()


@mcp.tool()
def propose_xrd_scan(low: float = 20, high: float = 70, step: float = .1, dwell: float = .02) -> dict:
    """Submit scan for human review. This tool never starts a scan or approves a plan."""
    return client.propose_scan(low,high,step,dwell)


@mcp.tool()
def execute_approved_plan(plan_id: str) -> dict:
    """Execute an unused plan approved by a human. May fail if expired or safety state changed."""
    return client.execute_approved_plan(plan_id)


@mcp.tool()
def stop_experiment() -> dict:
    """Cancel execution and request safe output confirmation. No human review needed to stop."""
    return client.stop_experiment()


@mcp.tool()
def request_emergency_stop() -> dict:
    """Latch software ESTOP. This does not prove a physical X-ray source is safe."""
    return client.request_emergency_stop()


if __name__ == '__main__':
    mcp.run(transport='stdio')
