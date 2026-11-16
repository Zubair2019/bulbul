"""One chat ID finds everything: debug endpoint and chat/run-tagged logs."""

import logging

from app import logctx
from app.schemas import SupervisorDecision
from tests.test_graph_flow import FakeRouter, make_client, post_sse


async def test_debug_endpoint_gathers_the_whole_chat():
    router = FakeRouter({"supervisor": [SupervisorDecision(action="respond"), SupervisorDecision(action="respond")]})
    client, stack = await make_client(router)
    async with stack, client:
        t = (await client.post("/api/threads", json={})).json()
        await post_sse(client, f"/api/threads/{t['id']}/runs", {"message": "hi"})
        await post_sse(client, f"/api/threads/{t['id']}/runs", {"message": "thanks"})

        d = (await client.get(f"/api/threads/{t['id']}/debug")).json()
        assert d["chat_id"] == t["id"] and d["short_id"] == t["id"][:8]
        assert d["how_to_debug"] == f"grep the backend logs for chat={t['id'][:8]}"
        assert [r["status"] for r in d["runs"]] == ["done", "done"]
        assert all(r["short_id"] == r["id"][:8] for r in d["runs"])
        assert [m["role"] for m in d["messages"]] == ["user", "assistant", "user", "assistant"]
        assert d["state"]["request_text"] == "thanks" and d["state"]["pending"] is None
        assert (await client.get("/api/threads/00000000-0000-0000-0000-000000000000/debug")).status_code == 404


async def test_log_lines_carry_chat_and_run(caplog):
    caplog.set_level(logging.INFO)
    caplog.handler.addFilter(logctx._ContextFilter())  # same filter the app installs on its handlers
    router = FakeRouter({"supervisor": [SupervisorDecision(action="respond")]})
    client, stack = await make_client(router)
    async with stack, client:
        t = (await client.post("/api/threads", json={})).json()
        ev = await post_sse(client, f"/api/threads/{t['id']}/runs", {"message": "hello there"})
        run_id = ev[0]["run_id"]
    tagged = [r for r in caplog.records if r.name == "app.graph.builder" and "supervisor decision" in r.getMessage()]
    assert tagged, "expected the supervisor's log line"
    assert (tagged[0].chat, tagged[0].run) == (t["id"][:8], run_id[:8])
    started = [r for r in caplog.records if "run started" in r.getMessage()]
    assert started and started[0].getMessage().startswith("run started (message): hello there")


def test_log_format_shows_chat_and_run():
    handler = logging.StreamHandler()
    logger = logging.getLogger("bulbul.test")
    handler.addFilter(logctx._ContextFilter())
    handler.setFormatter(logging.Formatter(logctx.FORMAT))
    record = logger.makeRecord("bulbul.test", logging.INFO, __file__, 1, "hello", None, None)
    with logctx.bind(chat="2534de8d-78a4-4af8-97b5-ebd73393f37c", run="91a0c3f2-0000-0000-0000-000000000000"):
        handler.filter(record)
    assert handler.format(record) == "INFO [chat=2534de8d run=91a0c3f2] bulbul.test: hello"
    record2 = logger.makeRecord("bulbul.test", logging.INFO, __file__, 1, "outside", None, None)
    handler.filter(record2)
    assert handler.format(record2) == "INFO [chat=- run=-] bulbul.test: outside"
