from __future__ import annotations

import numpy as np

from asr.backends.qwen_remote import QwenRemoteASRBackend, _extract_text


def test_qwen_remote_extracts_message_text() -> None:
    payload = {
        "output": {
            "choices": [
                {
                    "message": {
                        "content": [
                            {"text": "你好"},
                            {"text": "，世界"},
                        ],
                    },
                },
            ],
        },
    }
    assert _extract_text(payload) == "你好，世界"


def test_qwen_remote_transcribe_uses_upload_and_generation(
    monkeypatch,
) -> None:
    backend = QwenRemoteASRBackend(
        base_url="https://dashscope.example/api/v1",
        api_key="test-key",
        model="qwen3-asr-flash",
    )
    monkeypatch.setattr(
        backend,
        "_upload_audio",
        lambda session, file_path: "oss://unit-test.wav",
    )
    monkeypatch.setattr(
        backend,
        "_call_generation",
        lambda session, file_url, context: {
            "output": {
                "choices": [
                    {"message": {"content": [{"text": "测试通过"}]}},
                ],
            },
        },
    )

    result = backend.transcribe(
        np.zeros(1600, dtype=np.float32),
        sample_rate=16000,
        context="测试词汇",
    )

    assert result == "测试通过"
