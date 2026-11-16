"""Tracing helpers: masking, span filtering and naming (no Langfuse connection needed)."""

from types import SimpleNamespace

from app import tracing


def _params(attrs: dict):
    from langfuse.types import OtelSpanIdentifier

    ident = OtelSpanIdentifier(trace_id="0" * 32, span_id="0" * 16)
    return SimpleNamespace(spans={ident: SimpleNamespace(attributes=attrs)}), ident


def test_mask_redacts_credentials_only():
    params, ident = _params({
        "langfuse.observation.input": "my key is sk-proj-abcdefghijklmnopqrstuvwx and AIzaSyA1234567890abcdefghijklmnopqrstu",
        "langfuse.observation.output": "Here is your palak paneer recipe",
        "tags": ["ok", "Bearer abcdefghijklmnopqrstuvwxyz123"],
    })
    result = tracing.mask_otel_spans(params=params)
    patched = result.span_patches[ident].set_attributes
    assert "sk-proj" not in patched["langfuse.observation.input"]
    assert "AIza" not in patched["langfuse.observation.input"]
    assert patched["langfuse.observation.input"].count(tracing.REDACTED) == 2
    assert patched["tags"] == ["ok", tracing.REDACTED]
    assert "langfuse.observation.output" not in patched  # untouched attributes are not patched


def test_mask_leaves_clean_batches_alone():
    params, _ = _params({"langfuse.observation.input": "hey there!"})
    assert tracing.mask_otel_spans(params=params) is None


def test_names_are_stable_and_typed():
    assert tracing.generation_name("supervisor") == "decide-next-step"
    assert tracing.generation_name("responder") == "write-reply"
    assert tracing.generation_name("chef") == "generate-chef-result"
    # The LangChain handler types a chain as `agent` when its name contains "agent".
    assert "agent" in tracing.agent_run_name("chef")


def test_chat_turn_is_a_noop_when_disabled(monkeypatch):
    monkeypatch.setattr(tracing, "_enabled", False)
    with tracing.chat_turn(user_id="u", thread_id="t", run_id="r", kind="message",
                           user_text="hi", model=None, release="0") as turn:
        turn.finish("hello")
    assert turn.trace_id is None and tracing.callbacks() == []
