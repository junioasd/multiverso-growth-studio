import asyncio
import json
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

async def main():
    url = "http://127.0.0.1:8792/mcp"
    async with streamable_http_client(url) as streams:
        async with ClientSession(streams[0], streams[1]) as session:
            result = await session.initialize()
            print("INITIALIZE_OK=True")
            info = getattr(result, "serverInfo", None) or getattr(result, "server_info", None)
            if info:
                print("SERVER_NAME=" + str(info.name))
                print("SERVER_VERSION=" + str(info.version))
            tools = await session.list_tools()
            print("TOOLS=" + ",".join(t.name for t in tools.tools))
            health = await session.call_tool("atlas_health", arguments={})
            print("ATLAS_HEALTH_CALL_OK=True")
            err = getattr(health, "isError", None)
            if err is None:
                err = getattr(health, "is_error", None)
            print("IS_ERROR=" + str(err))
            structured = getattr(health, "structuredContent", None)
            if structured is None:
                structured = getattr(health, "structured_content", None)
            if structured is not None:
                print("STRUCTURED=" + json.dumps(structured, ensure_ascii=False))
            for item in (getattr(health, "content", None) or []):
                txt = getattr(item, "text", None)
                if txt is not None:
                    print("CONTENT_TEXT=" + txt)

asyncio.run(main())
