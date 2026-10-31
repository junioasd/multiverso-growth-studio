import asyncio, json, time
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

async def main():
    url = "http://127.0.0.1:8792/mcp"
    async with streamable_http_client(url) as streams:
        async with ClientSession(streams[0], streams[1]) as session:
            await session.initialize()
            args = {"task_type":"LIST_TEMPLATES","payload_json":"{}","preferred_agent":"SCRIPTOR","idempotency_key":"chat-atlas-scriptor-list-20261001-v1"}
            submitted = await session.call_tool("atlas_submit_task", arguments=args)
            print("SUBMIT=" + str(submitted))
            job_id = None
            for item in (getattr(submitted, "content", None) or []):
                txt = getattr(item, "text", None)
                if txt:
                    try:
                        data=json.loads(txt); job_id=data.get("job_id",job_id)
                    except Exception: pass
            print("JOB_ID=" + str(job_id))
            if not job_id: return
            for _ in range(20):
                got=await session.call_tool("atlas_get_task",arguments={"job_id":job_id})
                texts=[getattr(x,"text","") for x in (getattr(got,"content",None) or [])]
                print("GET=" + " ".join(texts))
                if any('"state":"COMPLETED"' in t.replace(" ","") or '"state": "COMPLETED"' in t for t in texts): return
                await asyncio.sleep(0.5)
asyncio.run(main())
