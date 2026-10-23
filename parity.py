"""Reproduce a DeepSeek prompt input-contract difference with pinned sources."""

from __future__ import annotations

import argparse
import contextlib
import copy
import difflib
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
from unittest.mock import patch
import urllib.request
import xml.etree.ElementTree as ET

from jinja2 import TemplateError
from jinja2.sandbox import ImmutableSandboxedEnvironment


ROOT = Path(__file__).resolve().parent
SOURCES = json.loads((ROOT / "sources.json").read_text())


class SourceError(RuntimeError):
    """A pinned source could not be obtained or failed its integrity check."""


def verify_source(data: bytes, spec: dict) -> None:
    if len(data) != spec["bytes"] or hashlib.sha256(data).hexdigest() != spec["sha256"]:
        raise SourceError("Pinned source integrity check failed: " + spec["url"])


def prepare_sources(cache: Path, offline: bool, upstream_tests: bool) -> None:
    """Fetch only allowlisted files, then verify before importing any source."""
    for relative, spec in SOURCES.items():
        if spec["group"] != "core" and not upstream_tests:
            continue
        destination = cache / relative
        if destination.exists():
            verify_source(destination.read_bytes(), spec)
            continue
        if offline:
            raise SourceError("Missing cached source in offline mode: " + relative)
        request = urllib.request.Request(spec["url"], headers={"User-Agent": "alphaBell-prompt-parity/0.1"})
        with urllib.request.urlopen(request, timeout=30) as response:
            data = response.read(spec["bytes"] + 1)
        verify_source(data, spec)
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_suffix(destination.suffix + ".part")
        temporary.write_bytes(data)
        temporary.replace(destination)


@contextlib.contextmanager
def deny_network():
    """Disable Python sockets while the downloaded references execute."""
    attempts = []

    def denied(*args, **kwargs):
        attempts.append("socket operation")
        raise RuntimeError("Network disabled during prompt rendering and tests")

    with patch.object(socket, "socket", denied), patch.object(socket, "create_connection", denied), patch.object(socket, "getaddrinfo", denied):
        yield attempts


def normalize_tool_arguments(messages: list) -> list:
    """Convert OpenAI JSON-string tool arguments to the template's object contract.

    Decode exactly once. Reject invalid JSON and non-object values. Preserve the
    caller's original history and every unrelated field.
    """
    normalized = copy.deepcopy(messages)
    for message in normalized:
        if message.get("role") != "assistant":
            continue
        for call in message.get("tool_calls") or []:
            function = call.get("function")
            if not isinstance(function, dict) or "arguments" not in function:
                continue
            arguments = function["arguments"]
            if isinstance(arguments, str):
                arguments = json.loads(arguments)
            if not isinstance(arguments, dict):
                raise ValueError("Tool arguments must decode to a JSON object")
            function["arguments"] = arguments
    return normalized


def load_renderers(cache: Path):
    spec = importlib.util.spec_from_file_location("deepseek_reference", cache / "reference/encoding/encoding.py")
    reference = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(reference)

    def raise_exception(message):
        raise TemplateError(message)

    environment = ImmutableSandboxedEnvironment(trim_blocks=True, lstrip_blocks=True)
    # Transformers' tojson serializes JSON without Jinja's HTML escaping or key sorting.
    environment.filters["tojson"] = lambda value, ensure_ascii=False, indent=None, separators=None, sort_keys=False: json.dumps(value, ensure_ascii=ensure_ascii, indent=indent, separators=separators, sort_keys=sort_keys)
    environment.globals["raise_exception"] = raise_exception
    template = environment.from_string((cache / "base/chat_template.jinja").read_text())
    return reference, template


def fixture_cases() -> list[dict]:
    history = [
        {"role": "user", "content": "question"},
        {"role": "assistant", "reasoning_content": "reason", "content": "summary", "tool_calls": [
            {"type": "function", "function": {"name": "lookup", "arguments": {"query": "value", "limit": 2}}}
        ]},
    ]
    raw = copy.deepcopy(history)
    raw[1]["tool_calls"][0]["function"]["arguments"] = '{"query":"value","limit":2}'
    simple = [{"role": "user", "content": "question"}]
    return [
        {"name": "basic_chat", "messages": simple, "mode": "chat", "effort": None, "expect_match": True},
        {"name": "numeric_effort_42", "messages": simple, "mode": "thinking", "effort": 42, "expect_match": True},
        {"name": "numeric_effort_62", "messages": simple, "mode": "thinking", "effort": 62, "expect_match": True},
        {"name": "mid_conversation_system", "messages": [
            {"role": "system", "content": "sys"}, {"role": "user", "content": "q1"},
            {"role": "assistant", "content": "a1", "reasoning_content": "r1"},
            {"role": "system", "content": "mid sys"},
        ], "mode": "thinking", "effort": 88, "expect_match": True},
        {"name": "object_arguments", "messages": history, "mode": "thinking", "effort": None, "expect_match": True},
        {"name": "raw_json_string_arguments", "messages": raw, "mode": "thinking", "effort": None, "expect_match": False},
        {"name": "normalized_json_string_arguments", "messages": normalize_tool_arguments(raw), "mode": "thinking", "effort": None, "expect_match": True},
        {"name": "medium_alias", "messages": simple, "mode": "thinking", "effort": "medium", "expect_match": False},
    ]


def compare_case(reference, template, case: dict) -> dict:
    result = {key: copy.deepcopy(case[key]) for key in ("name", "messages", "mode", "effort", "expect_match")}
    for name, render in [
        ("reference", lambda: reference.encode_messages(copy.deepcopy(case["messages"]), thinking_mode=case["mode"], reasoning_effort=case["effort"])),
        ("base_jinja", lambda: template.render(messages=copy.deepcopy(case["messages"]), thinking_mode=case["mode"], reasoning_effort=case["effort"], add_generation_prompt=True)),
    ]:
        try:
            result[name] = {"text": render()}
        except Exception as error:
            result[name] = {"exception": type(error).__name__, "message": str(error)}
    result["exact_match"] = result["reference"] == result["base_jinja"]
    result["expectation_met"] = result["exact_match"] == case["expect_match"] and (not case["expect_match"] or "text" in result["reference"] and "text" in result["base_jinja"])
    if "text" in result["reference"] and "text" in result["base_jinja"]:
        result["diff"] = "\n".join(difflib.unified_diff(result["reference"]["text"].splitlines(), result["base_jinja"]["text"].splitlines(), fromfile="1017-reference", tofile="base-jinja", lineterm=""))
    return result


def run_matrix(cache: Path) -> dict:
    identical = (cache / "base/encoding.py").read_bytes() == (cache / "reference/encoding/encoding.py").read_bytes()
    with deny_network() as attempts:
        reference, template = load_renderers(cache)
        cases = [compare_case(reference, template, case) for case in fixture_cases()]
    by_name = {case["name"]: case for case in cases}
    raw = by_name["raw_json_string_arguments"]
    medium = by_name["medium_alias"]
    details_verified = (
        '<｜DSML｜ parameter name="arguments" string="true">' in raw["base_jinja"].get("text", "")
        and '<｜DSML｜ parameter name="query" string="true">value' in raw["reference"].get("text", "")
        and '<｜DSML｜ parameter name="limit" string="false">2' in raw["reference"].get("text", "")
        and medium["reference"].get("exception") == "AssertionError"
        and "Reasoning Effort: 62 " in medium["base_jinja"].get("text", "")
    )
    return {"encoder_bytes_identical": identical, "network_attempts": attempts, "cases": cases, "specific_differences_verified": details_verified, "ok": identical and details_verified and not attempts and all(case["expectation_met"] for case in cases)}


def upstream_worker(cache: Path) -> int:
    os.environ["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    import pytest
    encoding_dir = cache / "reference/encoding"
    sys.path.insert(0, str(encoding_dir))
    with deny_network() as attempts:
        code = pytest.main(["-q", "--noconftest", "-p", "no:cacheprovider", str(encoding_dir / "test_encoding.py"), "--junitxml=" + str(cache / "upstream-tests.xml")])
    (cache / "upstream-network.json").write_text(json.dumps({"network_attempts": attempts}))
    return int(code)


def run_upstream_tests(cache: Path) -> dict:
    command = [sys.executable, str(ROOT / "parity.py"), "--upstream-worker", str(cache)]
    process = subprocess.run(command, text=True, capture_output=True, timeout=120)
    result = {"exit_code": process.returncode, "stdout": process.stdout, "stderr": process.stderr}
    xml = cache / "upstream-tests.xml"
    if xml.exists():
        suites = ET.parse(xml).getroot()
        result["tests"] = sum(int(s.get("tests", 0)) for s in suites.iter("testsuite"))
        result["failures"] = sum(int(s.get("failures", 0)) + int(s.get("errors", 0)) for s in suites.iter("testsuite"))
        result["skipped"] = sum(int(s.get("skipped", 0)) for s in suites.iter("testsuite"))
    audit = cache / "upstream-network.json"
    result["network_attempts"] = json.loads(audit.read_text())["network_attempts"] if audit.exists() else ["worker did not finish"]
    result["ok"] = process.returncode == 0 and result.get("tests", 0) > 0 and not result["network_attempts"]
    return result


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache-dir", type=Path, default=Path(".cache/deepseek-prompt-parity"))
    parser.add_argument("--offline", action="store_true", help="require all selected files to be cached and verified")
    parser.add_argument("--run-upstream-tests", action="store_true", help="also fetch and run the complete pinned encoder suite; needs pytest")
    parser.add_argument("--report", type=Path, help="write source pins, complete synthetic prompts and observed differences as JSON")
    parser.add_argument("--show-diff", action="store_true")
    parser.add_argument("--upstream-worker", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.upstream_worker:
        return upstream_worker(args.upstream_worker)
    cache = args.cache_dir.resolve()
    try:
        prepare_sources(cache, args.offline, args.run_upstream_tests)
        report = {"sources": SOURCES, "comparison": run_matrix(cache)}
        if args.run_upstream_tests:
            report["upstream_tests"] = run_upstream_tests(cache)
        if args.report:
            args.report.parent.mkdir(parents=True, exist_ok=True)
            args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
        comparison = report["comparison"]
        print("Base and 1017 Python encoder bytes identical:", comparison["encoder_bytes_identical"])
        for case in comparison["cases"]:
            print(case["name"] + ": " + ("MATCH" if case["exact_match"] else "EXPECTED DIFFERENCE" if case["expectation_met"] else "UNEXPECTED RESULT"))
            if args.show_diff and case.get("diff"):
                print(case["diff"])
        if "upstream_tests" in report:
            suite = report["upstream_tests"]
            print("Upstream suite:", suite.get("tests", 0), "tests;", suite.get("failures", "unknown"), "failures;", suite.get("skipped", "unknown"), "skipped")
        ok = comparison["ok"] and report.get("upstream_tests", {"ok": True})["ok"]
        print("All expected observations verified." if ok else "Verification failed; inspect the JSON report.")
        return 0 if ok else 1
    except (SourceError, OSError, ValueError, subprocess.SubprocessError) as error:
        print(str(error), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
