import copy
import hashlib
import json
from pathlib import Path
import socket
import io
import re
from urllib.parse import urlparse

import pytest

import parity


def history(arguments):
    return [{"role": "assistant", "content": None, "tool_calls": [{
        "id": "call-1", "type": "function", "function": {"name": "lookup", "arguments": arguments}
    }]}]


def test_normalization_preserves_original_history_and_nested_values():
    arguments = {"city": "東京", "seed": 9007199254740993, "nested": [True, None, {"x": "<tag>"}]}
    original = history(json.dumps(arguments, ensure_ascii=False))
    snapshot = copy.deepcopy(original)
    normalized = parity.normalize_tool_arguments(original)
    assert original == snapshot
    assert normalized[0]["tool_calls"][0]["function"]["arguments"] == arguments
    assert normalized[0]["tool_calls"][0]["id"] == "call-1"
    assert normalized[0]["content"] is None


@pytest.mark.parametrize("arguments", ["[]", "null", '"double-encoded"', "true", "12", "not-json"])
def test_non_object_or_invalid_json_arguments_are_rejected(arguments):
    with pytest.raises(ValueError):
        parity.normalize_tool_arguments(history(arguments))


def test_existing_objects_are_copied_without_coercion():
    messages = history({"n": 2, "s": "2"})
    normalized = parity.normalize_tool_arguments(messages)
    normalized[0]["tool_calls"][0]["function"]["arguments"]["n"] = 99
    assert messages[0]["tool_calls"][0]["function"]["arguments"] == {"n": 2, "s": "2"}


def test_other_message_fields_and_non_function_calls_are_preserved():
    messages = [{"role": "tool", "content": '{"not":"arguments"}'},
                {"role": "assistant", "tool_calls": [{"type": "custom", "custom": {"input": "text"}}]}]
    assert parity.normalize_tool_arguments(messages) == messages


def test_corrupted_cached_source_is_rejected_before_import(tmp_path):
    first_path = next(name for name, spec in parity.SOURCES.items() if spec["group"] == "core")
    path = tmp_path / first_path
    path.parent.mkdir(parents=True)
    path.write_bytes(b"raise RuntimeError('must never execute')")
    with pytest.raises(parity.SourceError, match="integrity"):
        parity.prepare_sources(tmp_path, offline=True, upstream_tests=False)


def test_missing_offline_source_never_attempts_download(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Offline mode attempted a download")
    monkeypatch.setattr(parity.urllib.request, "urlopen", forbidden)
    with pytest.raises(parity.SourceError, match="offline"):
        parity.prepare_sources(tmp_path, offline=True, upstream_tests=False)


def test_invalid_cold_cache_download_is_never_written(tmp_path, monkeypatch):
    monkeypatch.setattr(parity.urllib.request, "urlopen", lambda *args, **kwargs: io.BytesIO(b"untrusted replacement"))
    with pytest.raises(parity.SourceError, match="integrity"):
        parity.prepare_sources(tmp_path, offline=False, upstream_tests=False)
    assert list(tmp_path.rglob("*")) == []


def test_unknown_source_cli_option_is_rejected_before_fetch(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Unknown source option reached source preparation")
    monkeypatch.setattr(parity, "prepare_sources", forbidden)
    with pytest.raises(SystemExit) as error:
        parity.main(["--source", "https://example.invalid/untrusted.py"])
    assert error.value.code == 2


def test_manifest_allows_only_small_files_at_exact_official_revisions():
    for relative, spec in parity.SOURCES.items():
        url = urlparse(spec["url"])
        assert url.scheme == "https" and url.netloc == "huggingface.co"
        assert re.fullmatch(r"/deepseek-ai/DeepSeek-V4\.1-Flash(?:-1017)?/raw/[0-9a-f]{40}/(?:encoding|inference|chat_template\.jinja).*", url.path)
        assert ".." not in Path(relative).parts
        assert re.fullmatch(r"[0-9a-f]{64}", spec["sha256"])
        assert 0 < spec["bytes"] < 100_000


def test_integrity_checks_content_even_when_size_matches():
    expected = b"original"
    spec = {"url": "https://example.invalid/source", "bytes": len(expected), "sha256": hashlib.sha256(expected).hexdigest()}
    parity.verify_source(expected, spec)
    with pytest.raises(parity.SourceError):
        parity.verify_source(b"modified", spec)


def test_socket_operations_are_blocked_and_restored():
    original = socket.socket
    with parity.deny_network() as attempts:
        with pytest.raises(RuntimeError, match="Network disabled"):
            socket.create_connection(("example.invalid", 443))
    assert len(attempts) == 1
    assert socket.socket is original


def test_equal_errors_do_not_count_as_successful_prompt_parity():
    class BrokenReference:
        def encode_messages(self, *args, **kwargs):
            raise ValueError("both fail")
    class BrokenTemplate:
        def render(self, *args, **kwargs):
            raise ValueError("both fail")
    result = parity.compare_case(BrokenReference(), BrokenTemplate(), parity.fixture_cases()[0])
    assert result["exact_match"] is True
    assert result["expectation_met"] is False
