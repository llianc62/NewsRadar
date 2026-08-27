"""NewsRadarDaemon 结构级回归测试。

`run()` 是完整 daemon 生命周期(DB/信号/Web/worker/timer),无法纯单元测试。
此处用源码级断言守护关键不变量。
"""
import inspect


def test_run_starts_mcp_server():
    """回归:run() 必须启动 MCP Server。

    聊天室 per-session agent(_build_chat_agent) 调 create_agent(register_mcp=True)
    会连接 MCP Server,故 daemon 启动时 MCP 必须已启动,否则首个聊天请求 MCP 连接失败。
    """
    from main import NewsRadarDaemon
    src = inspect.getsource(NewsRadarDaemon.run).splitlines()
    assert any("_start_mcp_server" in ln for ln in src), \
        "run() 必须调用 _start_mcp_server(聊天室 agent 依赖 MCP)"
