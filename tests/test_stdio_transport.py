"""Check the installed module's real stdio interface without a platform connection."""

import asyncio
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


def test_stdio_tool_contract_and_missing_source(tmp_path):
    async def check():
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "research_hub", "stdio"],
            env={"RESEARCH_HUB_STATE_DIR": str(tmp_path)},
        )
        async with stdio_client(params) as (reader, writer):
            async with ClientSession(reader, writer) as session:
                await session.initialize()
                tools = {tool.name: tool for tool in (await session.list_tools()).tools}
                assert set(tools) == {
                    "list_research_tasks",
                    "get_research_progress",
                    "submit_research_guidance",
                    "get_research_guidance_status",
                }
                assert tools["submit_research_guidance"].annotations.readOnlyHint is False
                assert tools["get_research_guidance_status"].annotations.readOnlyHint is True
                result = await session.call_tool("list_research_tasks", {})
                assert not result.isError
                assert result.structuredContent["available"] is False
                assert result.structuredContent["reason"] == "snapshot_unavailable"
                result = await session.call_tool("get_research_progress", {})
                assert not result.isError
                assert result.structuredContent["live_research_progress_verified"] is False

    asyncio.run(check())
