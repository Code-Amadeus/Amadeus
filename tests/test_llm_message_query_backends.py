"""The cooperative role query keeps the existing Chat model choices."""

from __future__ import annotations

import json
import asyncio
import base64
import copy
import io
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import patch

import pytest


MESSAGES = [
    {"role": "system", "content": "Return JSON."},
    {"role": "user", "content": "Current fact"},
]


def _local_metadata(url, **kwargs):
    if url.endswith("/props"):
        data = {"default_generation_settings": {"n_ctx": 16384}}
    elif url.endswith("/apply-template"):
        assert kwargs["json"]["messages"]
        data = {"prompt": "templated messages"}
    elif url.endswith("/tokenize"):
        assert kwargs["json"]["content"] == "templated messages"
        data = {"tokens": [1, 2, 3]}
    else:
        raise AssertionError(url)
    return SimpleNamespace(raise_for_status=lambda: None, json=lambda: data)


def test_provider_change_drops_an_incompatible_openai_sdk_client() -> None:
    from llm import client

    stale = object()
    with (
        patch.object(client, "LLM_PROVIDER", "deepseek"),
        patch.object(client, "llm_client", stale),
    ):
        client.configure(llm_provider="hybrid2")
        assert client.llm_client is stale
        client.configure(llm_provider="openai")
        assert client.llm_client is None


def test_gemini_message_query_keeps_system_and_user_roles_separate() -> None:
    from llm import client

    calls = []

    def generate(model_client, **kwargs):
        calls.append((model_client, kwargs))
        return '{"say":"ok","action":null}'

    gemini = object()
    with (
        patch.object(client, "LLM_PROVIDER", "gemini"),
        patch.object(client, "gemini_model", gemini),
        patch.object(client, "generate_gemini_text", side_effect=generate),
    ):
        reply = client.remote_llm_messages_query(MESSAGES, max_tokens=321)

    assert reply == '{"say":"ok","action":null}'
    assert calls == [(gemini, {
        "model": client.GEMINI_MODEL_NAME,
        "contents": [{"role": "user", "parts": [{"text": "Current fact"}]}],
        "config": {
            "system_instruction": "Return JSON.",
            "temperature": 0.0,
            "max_output_tokens": 321,
            "response_mime_type": "application/json",
        },
    })]


@pytest.mark.parametrize("response_shape", ["content", "choices"])
def test_bedrock_message_query_uses_the_existing_native_transport(response_shape) -> None:
    from llm import client

    calls = []

    class Body:
        @staticmethod
        def read():
            if response_shape == "choices":
                return json.dumps({"choices": [{"message": {
                    "content": '{"say":"ok","action":null}',
                    "role": "assistant", "refusal": None}, "finish_reason": "stop"}]}).encode()
            return b'{"content":[{"text":"{\\"say\\":\\"ok\\",\\"action\\":null}"}]}'

    class Runtime:
        @staticmethod
        def invoke_model(**kwargs):
            calls.append(kwargs)
            return {"body": Body()}

    with (
        patch.object(client, "LLM_PROVIDER", "bedrock"),
        patch.object(client, "AWS_BEDROCK_AUTH_MODE", "boto3"),
        patch.object(client, "bedrock_runtime_client", Runtime()),
        patch.object(client, "init_llm_client", return_value="bedrock_client"),
    ):
        reply = client.remote_llm_messages_query(MESSAGES, max_tokens=222)

    assert reply == '{"say":"ok","action":null}'
    payload = json.loads(calls[0]["body"])
    assert payload["messages"] == MESSAGES
    assert payload["max_tokens"] == 222


def test_local_openai_compatible_message_query_preserves_messages() -> None:
    from llm import client

    seen = []

    class Response:
        @staticmethod
        def raise_for_status():
            return None

        @staticmethod
        def json():
            return {"choices": [{"message": {"content": "<think>x</think>{\"say\":\"ok\",\"action\":null}"}}]}

    def post(url, **kwargs):
        if not url.endswith("/chat/completions"):
            return _local_metadata(url, **kwargs)
        seen.append((url, kwargs))
        return Response()

    with (
        patch.object(client, "LLM_PROVIDER", "local"),
        patch.object(client, "LOCAL_LLM_TYPE", "llama_server"),
        patch.object(client.requests, "get", side_effect=_local_metadata),
        patch.object(client.requests, "post", side_effect=post),
    ):
        reply = client.remote_llm_messages_query(MESSAGES, max_tokens=111)

    assert reply == '{"say":"ok","action":null}'
    assert seen[0][1]["json"]["messages"] == MESSAGES
    assert seen[0][1]["json"]["max_tokens"] == 111
    assert seen[0][1]["json"]["response_format"] == {
        "type": "json_schema", "json_schema": {"name": "response", "schema": {"type": "object"}}}


def test_original_hybrid_uses_bedrock_for_one_coherent_role_reply() -> None:
    from llm import client

    with (
        patch.object(client, "LLM_PROVIDER", "hybrid"),
        patch.object(
            client,
            "_bedrock_messages_query",
            return_value='{"say":"ok","action":null}',
        ) as query,
    ):
        reply = client.remote_llm_messages_query(MESSAGES)

    assert reply == '{"say":"ok","action":null}'
    assert query.call_args.args == (MESSAGES,)


@pytest.mark.parametrize("provider", [
    "deepseek", "openai", "gemini", "bedrock", "local", "hybrid", "hybrid2", "hybrid3",
])
def test_focus_validator_system_reaches_backend_and_plain_set_is_recognized(provider):
    from llm import client
    from server.focus_policy import audit_focus_modifier

    calls = []
    attrs = {"intent": "execute", "focus": "set", "project_id": "project-a",
        "_host_source_user_text": "Switch to project A and create a checklist."}

    def create(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="SET"))])

    def generate(_client, **kwargs):
        calls.append(kwargs)
        return "SET"

    def invoke(**kwargs):
        calls.append(kwargs)
        return {"body": SimpleNamespace(read=lambda: b'{"content":[{"text":"SET"}]}')}

    def post(url, **kwargs):
        if not url.endswith("/chat/completions"):
            return _local_metadata(url, **kwargs)
        calls.append(kwargs)
        return SimpleNamespace(raise_for_status=lambda: None,
            json=lambda: {"choices": [{"message": {"content": "<think>confirm</think>SET"}}]})

    with (
        patch.object(client, "LLM_PROVIDER", provider),
        patch.object(client, "LOCAL_LLM_TYPE", "llama_server"),
        patch.object(client, "init_llm_client", return_value=None),
        patch.object(client, "llm_client", SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(create=create)))),
        patch.object(client, "gemini_model", object()),
        patch.object(client, "generate_gemini_text", side_effect=generate),
        patch.object(client, "AWS_BEDROCK_AUTH_MODE", "boto3"),
        patch.object(client, "bedrock_runtime_client", SimpleNamespace(invoke_model=invoke)),
        patch.object(client.requests, "get", side_effect=_local_metadata),
        patch.object(client.requests, "post", side_effect=post),
    ):
        audit = asyncio.run(audit_focus_modifier(attrs))

    assert audit.allowed and audit.decision == "set" and audit.outcome == "confirmed"
    assert len(calls) == 1
    request = calls[0]
    if provider == "gemini":
        system = request["config"]["system_instruction"]
        payload = request["contents"][0]["parts"][0]["text"]
        assert request["contents"][0]["role"] == "user"
        assert request["config"]["temperature"] == 0.0
        assert request["config"]["max_output_tokens"] >= 900
        assert "response_mime_type" not in request["config"]
    else:
        transport = json.loads(request["body"]) if provider in {"bedrock", "hybrid"} else (
            request["json"] if provider == "local" else request)
        messages = transport["messages"]
        assert [message["role"] for message in messages] == ["system", "user"]
        system, payload = messages[0]["content"], messages[1]["content"]
        assert "response_format" not in transport
        token_key = "max_completion_tokens" if provider in {"openai", "hybrid3"} else "max_tokens"
        assert transport[token_key] >= 900
        if provider not in {"openai", "hybrid3"}:
            assert transport["temperature"] == 0.0
        if provider not in {"bedrock", "hybrid"}:
            assert request["timeout"] == 10.0
    assert system.startswith("You are a narrow control-plane validator.")
    assert "exactly one token: SET, CLEAR, or NONE" in system
    assert json.loads(payload) == {"user_message": attrs["_host_source_user_text"],
        "proposed_focus": "set", "operation_intent": "execute", "project_id_present": True}


def _visual_context():
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (2, 3), "red").save(buffer, format="PNG")
    return {
        "reason": "attachment",
        "scope": "user_image",
        "attachment": {"name": "sample.png"},
        "frame": {
            "mime": "image/png",
            "dataBase64": base64.b64encode(buffer.getvalue()).decode("ascii"),
        },
    }


@pytest.mark.parametrize("provider", ["openai", "hybrid3", "deepseek", "hybrid2"])
@pytest.mark.parametrize("with_image", [False, True])
def test_sdk_message_query_preserves_visual_payload_and_json_contract(provider, with_image):
    from llm import client
    from llm.visual_context import visual_notice_text

    messages = MESSAGES[:1] + [
        {"role": "user", "content": "Earlier question"},
        {"role": "assistant", "content": "Earlier reply"},
        MESSAGES[1],
    ]
    original = copy.deepcopy(messages)
    visual = _visual_context() if with_image else None
    calls = []

    def create(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(content='{"say":"ok","action":null}'),
        )])

    sdk = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    with (
        patch.object(client, "LLM_PROVIDER", provider),
        patch.object(client, "DEEPSEEK_MODEL_NAME", "deepseek-flash"),
        patch.object(client, "llm_client", sdk),
    ):
        reply = client.remote_llm_messages_query(messages, visual_context=visual)

    assert reply == '{"say":"ok","action":null}'
    assert messages == original
    sent = calls[0]["messages"]
    assert sent[:-1] == original[:-1]
    assert calls[0]["response_format"] == {"type": "json_object"}
    if not with_image:
        assert sent == original
    else:
        assert sent[-1]["role"] == "user"
        assert sent[-1]["content"] == [
            {"type": "text", "text": visual_notice_text("Current fact", visual, supported=True)},
            {"type": "image_url", "image_url": {
                "url": "data:image/png;base64," + visual["frame"]["dataBase64"],
                "detail": "auto",
            }},
        ]


@pytest.mark.parametrize("provider", ["deepseek", "hybrid2"])
@pytest.mark.parametrize("model,supported", [
    ("deepseek-flash", True),
    ("deepseek-v4-flash", True),
    ("deepseek-v4-flash-vision-exp", True),
    ("deepseek-v4-pro", False),
    ("deepseek-chat", False),
])
def test_deepseek_image_capability_follows_requested_model(monkeypatch, provider, model, supported):
    from llm import client

    calls = []

    def create(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(content="ok"),
        )])

    monkeypatch.setattr(client, "LLM_PROVIDER", provider)
    # The per-request model override is authoritative, in both directions.
    monkeypatch.setattr(client, "DEEPSEEK_MODEL_NAME", "deepseek-v4-pro" if supported else "deepseek-flash")
    monkeypatch.setattr(client, "llm_client", SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create)),
    ))
    client.remote_llm_messages_query(MESSAGES, model=model, visual_context=_visual_context())
    assert calls[0]["model"] == model
    content = calls[0]["messages"][-1]["content"]
    if supported:
        assert content[1]["type"] == "image_url"
    else:
        assert "[VISUAL_CONTEXT_UNAVAILABLE]" in content


def test_gemini_image_uses_sdk_encoding_and_preserves_conversation_roles():
    from PIL import Image
    from llm import client
    from llm.visual_context import visual_notice_text

    visual = _visual_context()
    messages = MESSAGES[:1] + [
        {"role": "user", "content": "Earlier question"},
        {"role": "assistant", "content": "Earlier reply"},
        MESSAGES[1],
    ]
    original = copy.deepcopy(messages)
    calls = []

    def generate_content(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(text='{"say":"ok","action":null}')

    sdk = SimpleNamespace(models=SimpleNamespace(generate_content=generate_content))
    with patch.object(client, "LLM_PROVIDER", "gemini"), patch.object(client, "gemini_model", sdk):
        reply = client.remote_llm_messages_query(messages, visual_context=visual)

    assert reply == '{"say":"ok","action":null}'
    assert messages == original
    config = calls[0]["config"]
    assert config["system_instruction"] == "Return JSON."
    assert config["response_mime_type"] == "application/json"
    contents = calls[0]["contents"]
    assert contents[:2] == [
        {"role": "user", "parts": [{"text": "Earlier question"}]},
        {"role": "model", "parts": [{"text": "Earlier reply"}]},
    ]
    assert contents[-1].role == "user"
    text, image = contents[-1].parts
    assert text.text == visual_notice_text("Current fact", visual, supported=True)
    assert image.inline_data.mime_type == "image/png"
    decoded = Image.open(io.BytesIO(image.inline_data.data))
    assert decoded.size == (2, 3)
    assert decoded.getpixel((0, 0)) == (255, 0, 0)


@pytest.mark.parametrize("provider", ["bedrock", "hybrid", "local"])
def test_text_only_transport_explains_visual_limit_without_rejecting_chat(provider):
    from llm import client
    from llm.visual_context import visual_notice_text

    visual = _visual_context()
    messages = copy.deepcopy(MESSAGES)
    transport = "_local_messages_query" if provider == "local" else "_bedrock_messages_query"
    with patch.object(client, "LLM_PROVIDER", provider), patch.object(
        client, transport, return_value='{"say":"ok","action":null}',
    ) as query:
        reply = client.remote_llm_messages_query(messages, visual_context=visual)

    assert reply == '{"say":"ok","action":null}'
    assert messages == MESSAGES
    assert query.call_args.args[0] == [MESSAGES[0], {
        "role": "user",
        "content": visual_notice_text("Current fact", visual, supported=False),
    }]


@pytest.mark.parametrize("provider", ["openai", "hybrid3", "gemini", "deepseek", "hybrid2"])
def test_multimodal_capture_failure_uses_existing_error_notice(provider):
    from llm import client
    from llm.visual_context import visual_notice_text

    visual = {"error": "capture failed"}
    expected = visual_notice_text("Current fact", visual, supported=False)
    calls = []

    def create(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="{}"))])

    def generate(model_client, **kwargs):
        calls.append(kwargs)
        return "{}"

    with ExitStack() as stack:
        stack.enter_context(patch.object(client, "LLM_PROVIDER", provider))
        stack.enter_context(patch.object(client, "DEEPSEEK_MODEL_NAME", "deepseek-flash"))
        stack.enter_context(patch.object(client, "gemini_model", object()))
        stack.enter_context(patch.object(client, "generate_gemini_text", side_effect=generate))
        stack.enter_context(patch.object(client, "llm_client", SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(create=create))),
        ))
        client.remote_llm_messages_query(MESSAGES, visual_context=visual)

    if provider == "gemini":
        assert calls[0]["contents"][-1] == {"role": "user", "parts": [{"text": expected}]}
    else:
        assert calls[0]["messages"][-1] == {"role": "user", "content": expected}
    assert MESSAGES[1]["content"] == "Current fact"


class _Stream:
    def __init__(self, chunks):
        self.chunks = iter(chunks)
        self.closed = False
        self.consumed = 0

    def __iter__(self):
        return self

    def __next__(self):
        self.consumed += 1
        return next(self.chunks)

    def close(self):
        self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


def _aws_frame(payload):
    """A real AWS EventStream frame, including both CRCs."""
    import struct
    import zlib

    body = json.dumps({"bytes": base64.b64encode(json.dumps(payload).encode()).decode()}).encode()
    prelude = struct.pack(">II", len(body) + 16, 0)
    message = prelude + struct.pack(">I", zlib.crc32(prelude)) + body
    return message + struct.pack(">I", zlib.crc32(message))


@pytest.mark.parametrize("backend", [
    "openai", "deepseek", "hybrid2", "hybrid3", "gemini",
    "bedrock", "hybrid", "bedrock-bearer", "bedrock-bearer-pooled",
    "ollama", "lmstudio", "llama_server",
])
@pytest.mark.parametrize("failure", [None, RuntimeError, asyncio.CancelledError])
@pytest.mark.parametrize("json_output", [True, False])
def test_message_stream_is_one_request_and_closes_on_callback_failure(backend, failure, json_output, caplog):
    from llm import client

    caplog.set_level("INFO", logger="llm.client")

    parts = ['{"action":null,"say":"', 'Hello。', '"}'] if json_output else ['完了したわ。', '結果は', 'こちら。']
    calls = []
    observed = []
    delivered = []
    visual = _visual_context()
    messages = copy.deepcopy(MESSAGES)
    sdk_backends = {"openai", "deepseek", "hybrid2", "hybrid3"}
    local_backends = {"ollama", "lmstudio", "llama_server"}
    bearer = backend.startswith("bedrock-bearer")
    provider = "local" if backend in local_backends else "bedrock" if bearer else backend

    if backend in sdk_backends:
        chunks = [SimpleNamespace(
            model="observed-model", id="response-id",
            choices=[SimpleNamespace(delta=SimpleNamespace(content=part), finish_reason=None)],
        ) for part in parts]
        chunks.append(SimpleNamespace(
            model="observed-model", id="response-id",
            choices=[SimpleNamespace(delta=SimpleNamespace(content=None), finish_reason="stop")],
        ))
    elif backend == "gemini":
        chunks = [SimpleNamespace(text=part) for part in parts]
    elif backend in {"bedrock", "hybrid"} or bearer:
        payloads = [{"type": "content_block_delta", "delta": {"text": part}} for part in parts]
        if bearer:
            chunks = [_aws_frame(payload) for payload in payloads]
        else:
            chunks = [{"chunk": {"bytes": json.dumps(payload).encode()}} for payload in payloads]
    elif backend == "ollama":
        chunks = [json.dumps({"message": {"content": part}, "done": False}).encode() for part in parts]
    else:
        chunks = [('data: ' + json.dumps({"choices": [{"delta": {"content": part}}]})).encode() for part in parts]
    stream = _Stream(chunks)

    def create(*args, **kwargs):
        if backend == "llama_server" and args and not args[0].endswith("/chat/completions"):
            return _local_metadata(*args, **kwargs)
        calls.append(kwargs)
        if backend in {"bedrock", "hybrid"}:
            return {"body": stream}
        return stream

    stream.raise_for_status = lambda: None
    stream.iter_lines = lambda: iter(stream)
    stream.iter_content = lambda **kwargs: iter(stream)
    stream.iter_bytes = lambda: iter(stream)

    def on_text(text):
        # Each callback runs before pulling the next token, not after buffering.
        assert stream.consumed == len(delivered) + 1
        delivered.append(text)
        if failure is not None:
            raise failure("stop this query")

    with ExitStack() as stack:
        stack.enter_context(patch.object(client, "LLM_PROVIDER", provider))
        stack.enter_context(patch.object(client, "init_llm_client", return_value=None))
        stack.enter_context(patch.object(client, "DEEPSEEK_MODEL_NAME", "deepseek-flash"))
        stack.enter_context(patch.object(client, "llm_client", SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(create=create)),
        )))
        stack.enter_context(patch.object(client, "gemini_model", SimpleNamespace(
            models=SimpleNamespace(generate_content_stream=create),
        )))
        stack.enter_context(patch.object(client, "LOCAL_LLM_TYPE", backend))
        stack.enter_context(patch.object(client.requests, "get", side_effect=_local_metadata))
        stack.enter_context(patch.object(client.requests, "post", side_effect=create))
        stack.enter_context(patch.object(client, "AWS_BEDROCK_AUTH_MODE", "bearer" if bearer else "auto"))
        stack.enter_context(patch.object(client, "AWS_BEDROCK_BEARER_TOKEN", "test-token"))
        stack.enter_context(patch.object(client, "bedrock_http_client", SimpleNamespace(
            stream=create,
        ) if backend == "bedrock-bearer-pooled" else None))
        stack.enter_context(patch.object(client, "bedrock_runtime_client", SimpleNamespace(
            invoke_model_with_response_stream=create,
        )))
        if failure is None:
            reply = client.remote_llm_messages_query(
                messages, on_text=on_text, visual_context=visual, response_observer=observed.append,
                json_output=json_output,
                turn_id="timed-turn",
            )
            assert reply == "".join(parts)
            assert delivered == parts
            assert observed[0]["content"] == reply
            if backend in sdk_backends:
                assert observed[0]["response_model"] == "observed-model"
                assert observed[0]["response_id"] == "response-id"
                assert observed[0]["finish_reason"] == "stop"
        else:
            with pytest.raises(failure, match="stop this query"):
                client.remote_llm_messages_query(messages, on_text=on_text, visual_context=visual,
                    json_output=json_output, turn_id="timed-turn")
            assert stream.consumed == 1
    assert len(calls) == 1
    assert stream.closed
    timing = [record.getMessage() for record in caplog.records
        if "[MODEL-LATENCY]" in record.getMessage()]
    assert len(timing) == 2
    assert "stage=request_start" in timing[0] and "stage=first_text" in timing[1]
    assert all("turn_id=timed-turn" in line for line in timing)
    assert timing[0].split("request_id=")[1].split()[0] == timing[1].split("request_id=")[1].split()[0]
    assert all(part not in line for part in parts for line in timing)
    assert messages == MESSAGES
    if backend in sdk_backends:
        assert calls[0]["stream"] is True
        assert ("response_format" in calls[0]) is json_output
    elif backend in local_backends:
        assert calls[0]["stream"] is True
        assert calls[0]["json"]["stream"] is True
        if backend == "ollama":
            assert ("format" in calls[0]["json"]) is json_output
        else:
            assert ("response_format" in calls[0]["json"]) is json_output
    elif backend in {"bedrock", "hybrid"}:
        assert json.loads(calls[0]["body"])["stream"] is True
    elif bearer:
        assert calls[0]["json"]["stream"] is True
    if backend in sdk_backends:
        assert calls[0]["messages"][-1]["content"][1]["type"] == "image_url"
        if json_output:
            assert calls[0]["response_format"] == {"type": "json_object"}
    if backend == "gemini":
        assert calls[0]["contents"][-1].parts[1].inline_data.mime_type == "image/png"
        assert ("response_mime_type" in calls[0]["config"]) is json_output


def test_model_timing_excludes_client_preparation_and_ignores_empty_chunks(monkeypatch, caplog):
    from llm import client

    caplog.set_level("INFO", logger="llm.client")
    ticks = iter([10.0, 10.1, 10.35, 20.0, 20.2, 20.7])
    monkeypatch.setattr(client, "time", SimpleNamespace(perf_counter=lambda:next(ticks)))
    monkeypatch.setattr(client, "LLM_PROVIDER", "deepseek")

    def create(**kwargs):
        assert "turn_id" not in kwargs
        return _Stream([SimpleNamespace(choices=[SimpleNamespace(
            delta=SimpleNamespace(content=part), finish_reason=None)])
            for part in (None, "", "first", "second")])

    monkeypatch.setattr(client, "llm_client", SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create))))
    for turn in ("one", "two"):
        assert client.remote_llm_messages_query(MESSAGES, turn_id=turn,
            on_text=lambda _:None) == "firstsecond"
    timing = [record.getMessage() for record in caplog.records if "[MODEL-LATENCY]" in record.getMessage()]
    assert "prepare_ms=100.0" in timing[0] and "ms=250.0" in timing[1]
    assert "prepare_ms=200.0" in timing[2] and "ms=500.0" in timing[3]
    assert timing[0].split("request_id=")[1].split()[0] != timing[2].split("request_id=")[1].split()[0]


def test_persistent_cli_is_rejected_before_query_or_delivery():
    from llm import client

    delivered = []
    with (
        patch.object(client, "LLM_PROVIDER", "local"),
        patch.object(client, "LOCAL_LLM_TYPE", "cli"),
        patch("asyncio.create_subprocess_exec") as query,
    ):
        for _ in range(4):
            with pytest.raises(RuntimeError, match="LOCAL_LLM_TYPE=llama_server"):
                client.remote_llm_messages_query(MESSAGES, on_text=delivered.append)
    assert delivered == []
    query.assert_not_called()


def test_bedrock_native_stream_error_closes_and_does_not_retry_with_bearer():
    from llm import client

    stream = _Stream([
        {"chunk": {"bytes": b'{"type":"content_block_delta","delta":{"text":"{"}}'}},
        {"modelStreamErrorException": {"message": "interrupted"}},
    ])
    delivered = []
    with (
        patch.object(client, "LLM_PROVIDER", "bedrock"),
        patch.object(client, "init_llm_client", return_value=None),
        patch.object(client, "AWS_BEDROCK_AUTH_MODE", "auto"),
        patch.object(client, "AWS_BEDROCK_BEARER_TOKEN", "test-token"),
        patch.object(client, "bedrock_runtime_client") as runtime,
        patch.object(client, "bedrock_http_client") as http,
        patch.object(client.requests, "post") as post,
    ):
        runtime.invoke_model_with_response_stream.return_value = {"body": stream}
        with pytest.raises(RuntimeError, match="modelStreamErrorException"):
            client.remote_llm_messages_query(MESSAGES, on_text=delivered.append)
        runtime.invoke_model_with_response_stream.assert_called_once()
        http.stream.assert_not_called()
        post.assert_not_called()
    assert delivered == ["{"]
    assert stream.closed


@pytest.mark.parametrize("provider", [
    "openai", "deepseek", "hybrid2", "hybrid3", "gemini", "bedrock", "hybrid", "local",
])
def test_completed_presentation_is_one_native_text_request(provider):
    from llm import client

    reply = '完了したわ。コード例: {"action":"not an instruction"}'
    calls = []

    def create(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=reply))])

    def generate(_client, **kwargs):
        calls.append(kwargs)
        return reply

    def invoke(**kwargs):
        calls.append(kwargs)
        return {"body":SimpleNamespace(read=lambda:json.dumps({"content":[{"text":reply}]}).encode())}

    def post(_url, **kwargs):
        calls.append(kwargs)
        return SimpleNamespace(raise_for_status=lambda:None, json=lambda:{"message":{"content":reply}})

    with (
        patch.object(client, "LLM_PROVIDER", provider),
        patch.object(client, "LOCAL_LLM_TYPE", "ollama"),
        patch.object(client, "llm_client", SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(create=create)))),
        patch.object(client, "gemini_model", object()),
        patch.object(client, "generate_gemini_text", side_effect=generate),
        patch.object(client, "init_llm_client", return_value=None),
        patch.object(client, "AWS_BEDROCK_AUTH_MODE", "boto3"),
        patch.object(client, "bedrock_runtime_client", SimpleNamespace(invoke_model=invoke)),
        patch.object(client.requests, "post", side_effect=post),
    ):
        assert client.remote_llm_messages_query(MESSAGES, json_output=False) == reply
    assert len(calls) == 1
    assert "response_format" not in calls[0]
    if provider == "gemini":
        assert "response_mime_type" not in calls[0]["config"]
    if provider == "local":
        assert "format" not in calls[0]["json"]
