"""Gemini thinking tokens are counted as output tokens and priced (#4798).

gemini-cli's stream-json ``result`` stats report ``output_tokens`` as the
candidates count only; thinking tokens show up just in ``total_tokens``.

Module under test: docker/base-image/agent_server/services/gemini_runtime.py
"""
from __future__ import annotations

import json

import pytest

from agent_server.models import ExecutionMetadata
from agent_server.services import gemini_runtime

pytestmark = pytest.mark.unit

_MODEL = "gemini-2.5-pro"


def _parse(stats):
    metadata = ExecutionMetadata()
    gemini_runtime.GeminiRuntime()._process_stream_line(
        json.dumps({"type": "result", "status": "success", "stats": stats}),
        [], metadata, {}, {}, [], _MODEL,
    )
    return metadata


def test_thinking_tokens_are_counted_and_priced_as_output():
    # 1000 prompt + 200 candidates + 800 thoughts
    metadata = _parse({"input_tokens": 1000, "output_tokens": 200, "total_tokens": 2000})

    assert metadata.input_tokens == 1000
    assert metadata.output_tokens == 1000
    assert metadata.cost_usd == gemini_runtime.calculate_gemini_cost(1000, 1000, _MODEL)


def test_stats_without_total_tokens_keep_output_tokens():
    metadata = _parse({"input_tokens": 1000, "output_tokens": 200})

    assert metadata.output_tokens == 200
