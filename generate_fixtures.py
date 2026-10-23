"""Generate the browser's saved synthetic cases from the checksum-pinned CLI probes."""

from __future__ import annotations

import argparse
import copy
import json
from pathlib import Path
import sys

import jinja2

import parity


CASE_DETAILS = {
    "basic_chat": {
        "title": "Simple chat",
        "summary": "Chat mode produces the same serialized prompt in both renderers.",
        "normalization": {"applied": False, "explanation": "No tool arguments are present. The history is passed through unchanged.", "related_case_id": None},
    },
    "numeric_effort_42": {
        "title": "Reasoning effort: 42",
        "summary": "An explicit numeric effort of 42 produces matching prompts.",
        "normalization": {"applied": False, "explanation": "The integer 42 is passed directly to both renderers.", "related_case_id": None},
    },
    "numeric_effort_62": {
        "title": "Reasoning effort: 62",
        "summary": "The integer 62 matches, unlike the string alias in the saved medium case.",
        "normalization": {"applied": False, "explanation": "The integer 62 is supplied explicitly. The harness does not translate an alias.", "related_case_id": "medium_alias"},
    },
    "mid_conversation_system": {
        "title": "A system message after a turn",
        "summary": "Both renderers preserve the later system message and append the assistant generation header.",
        "normalization": {"applied": False, "explanation": "The same four-message history is passed to both renderers without conversion.", "related_case_id": None},
    },
    "object_arguments": {
        "title": "Tool arguments as an object",
        "summary": "Object arguments preserve separate string and numeric DSML parameters.",
        "normalization": {"applied": False, "explanation": "Arguments are already an object, matching the Transformers template input contract.", "related_case_id": "raw_json_string_arguments"},
    },
    "raw_json_string_arguments": {
        "title": "Tool arguments as a JSON string",
        "summary": "The template emits one arguments parameter. The Python reference decodes the string into query and limit.",
        "normalization": {"applied": False, "explanation": "The OpenAI-style JSON string reaches the template directly. A serving adapter may already convert it before rendering.", "related_case_id": "normalized_json_string_arguments"},
    },
    "normalized_json_string_arguments": {
        "title": "The same history, normalized",
        "summary": "Decoding the argument string once into an object restores exact prompt parity in this probe.",
        "normalization": {"applied": True, "explanation": "normalize_tool_arguments() deep-copies the raw-string history and decodes function.arguments once into an object. Unrelated fields are preserved.", "related_case_id": "raw_json_string_arguments"},
    },
    "medium_alias": {
        "title": 'The "medium" alias',
        "summary": "The Jinja artifact maps medium to 62. The Python reference rejects this alias.",
        "normalization": {"applied": False, "explanation": "The string medium is passed unchanged. Compare the explicit integer 62 case for the matching control.", "related_case_id": "numeric_effort_62"},
    },
}

SOURCE_DETAILS = {
    "reference/encoding/encoding.py": ("reference", "1017 Python reference"),
    "base/encoding.py": ("base_reference", "Base Python reference"),
    "base/chat_template.jinja": ("base_template", "Base Jinja artifact"),
}


def output_record(record: dict) -> dict:
    if "text" in record:
        return {"output": record["text"], "error": None}
    return {"output": None, "error": {"type": record["exception"], "message": record["message"]}}


def build_document(comparison: dict) -> dict:
    if not comparison.get("ok") or comparison.get("network_attempts"):
        raise ValueError("The pinned probe verification did not pass; fixtures were not generated")
    rows = comparison["cases"]
    if len(rows) != len(CASE_DETAILS) or {row["name"] for row in rows} != set(CASE_DETAILS):
        raise ValueError("Only the eight declared synthetic probes may enter browser fixtures")
    sources = []
    for relative, (identifier, label) in SOURCE_DETAILS.items():
        spec = parity.SOURCES[relative]
        sources.append({"id": identifier, "label": label, "url": spec["url"], "sha256": spec["sha256"], "revision": spec["url"].split("/")[6]})
    cases = []
    for row in rows:
        reference = output_record(row["reference"])
        template = output_record(row["base_jinja"])
        observed_match = reference["output"] is not None and template["output"] is not None and reference["output"] == template["output"]
        if not row["expectation_met"] or observed_match != row["expect_match"]:
            raise ValueError("Unexpected observation for " + row["name"])
        cases.append({
            "id": row["name"],
            **copy.deepcopy(CASE_DETAILS[row["name"]]),
            "history": copy.deepcopy(row["messages"]),
            "config": {"thinking_mode": row["mode"], "reasoning_effort": row["effort"], "add_generation_prompt": True},
            "reference": reference,
            "template": template,
            "expected_match": row["expect_match"],
            "observed_match": observed_match,
            "diff": row.get("diff", ""),
        })
    return {
        "schema_version": 1,
        "title": "DeepSeek Prompt Parity",
        "scope": "Eight saved synthetic serialization probes. Inspect inputs, prompt text and input-contract differences. No model inference or arbitrary-input validation.",
        "sources": sources,
        "encoder_bytes_identical": comparison["encoder_bytes_identical"],
        "rendering_environment": {"jinja2": "3.1.6", "sandbox": "ImmutableSandboxedEnvironment", "trim_blocks": True, "lstrip_blocks": True, "tojson": "Transformers-style JSON serialization"},
        "cases": cases,
    }


def serialize(document: dict) -> str:
    return json.dumps(document, ensure_ascii=False, indent=2) + "\n"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache-dir", type=Path, default=Path(".cache/deepseek-prompt-parity"))
    parser.add_argument("--offline", action="store_true")
    parser.add_argument("--check", action="store_true", help="fail if checked-in fixtures differ; write nothing")
    parser.add_argument("--output", type=Path, default=parity.ROOT / "fixtures.json")
    args = parser.parse_args(argv)
    try:
        if jinja2.__version__ != "3.1.6":
            raise ValueError("Fixture generation requires Jinja2 3.1.6")
        parity.prepare_sources(args.cache_dir.resolve(), args.offline, upstream_tests=False)
        document = build_document(parity.run_matrix(args.cache_dir.resolve()))
        expected = serialize(document)
        if args.check:
            if not args.output.exists() or args.output.read_text() != expected:
                print("Saved fixtures differ from the pinned probes. Run generate_fixtures.py to regenerate.", file=sys.stderr)
                return 1
            print("Eight saved cases match the checksum-pinned CLI probes.")
            return 0
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(expected)
        print("Generated eight saved synthetic cases.")
        return 0
    except (OSError, ValueError, parity.SourceError) as error:
        print(str(error), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
