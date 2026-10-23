import copy
import json

import pytest

import generate_fixtures as generator
import parity


def recorded_comparison():
    """Use the saved record only to exercise generator guards, without downloads."""
    document = json.loads((parity.ROOT / "fixtures.json").read_text())
    rows = []
    for case in document["cases"]:
        row = {
            "name": case["id"], "messages": case["history"],
            "mode": case["config"]["thinking_mode"], "effort": case["config"]["reasoning_effort"],
            "expect_match": case["expected_match"], "expectation_met": True, "diff": case["diff"],
        }
        for key, record in [("reference", case["reference"]), ("base_jinja", case["template"])]:
            row[key] = {"text": record["output"]} if record["error"] is None else {
                "exception": record["error"]["type"], "message": record["error"]["message"],
            }
        rows.append(row)
    return {"ok": True, "network_attempts": [], "encoder_bytes_identical": True, "cases": rows}


def test_prompt_text_and_error_messages_remain_literal():
    text = '  <｜DSML｜ calls>\n\tparameter  \n'
    assert generator.output_record({"text": text}) == {"output": text, "error": None}
    error = {"exception": "ValueError", "message": "  message with spacing\n"}
    assert generator.output_record(error)["error"]["message"] == error["message"]


def test_failed_reference_verification_cannot_generate_fixtures():
    comparison = recorded_comparison()
    comparison["ok"] = False
    with pytest.raises(ValueError, match="did not pass"):
        generator.build_document(comparison)


def test_unexpected_cases_are_rejected_even_if_report_claims_success():
    comparison = recorded_comparison()
    comparison["cases"][0]["name"] = "unreviewed-case"
    with pytest.raises(ValueError, match="eight declared synthetic probes"):
        generator.build_document(comparison)


def test_equal_errors_cannot_become_a_saved_parity_match():
    comparison = recorded_comparison()
    comparison["cases"][0]["reference"] = {"exception": "ValueError", "message": "same"}
    comparison["cases"][0]["base_jinja"] = {"exception": "ValueError", "message": "same"}
    with pytest.raises(ValueError, match="Unexpected observation"):
        generator.build_document(comparison)


def test_network_attempts_prevent_fixture_publication():
    comparison = recorded_comparison()
    comparison["network_attempts"] = ["socket operation"]
    with pytest.raises(ValueError, match="did not pass"):
        generator.build_document(comparison)


def test_generator_does_not_mutate_inputs_or_include_unselected_report_fields():
    comparison = recorded_comparison()
    comparison["private_log_path"] = "/private/runtime/log"
    snapshot = copy.deepcopy(comparison)
    document = generator.build_document(comparison)
    assert comparison == snapshot
    assert "private_log_path" not in document
    assert "/private/runtime/log" not in generator.serialize(document)
    assert len(document["sources"]) == 3
    assert "generated_at" not in document


def test_check_detects_drift_without_overwriting_file(tmp_path, monkeypatch):
    comparison = recorded_comparison()
    output = tmp_path / "fixtures.json"
    output.write_text('{"drift":true}\n')
    before = output.read_bytes()
    monkeypatch.setattr(parity, "prepare_sources", lambda *args, **kwargs: None)
    monkeypatch.setattr(parity, "run_matrix", lambda *args, **kwargs: comparison)
    assert generator.main(["--offline", "--check", "--output", str(output)]) == 1
    assert output.read_bytes() == before


def test_check_accepts_exact_deterministic_bytes(tmp_path, monkeypatch):
    comparison = recorded_comparison()
    output = tmp_path / "fixtures.json"
    output.write_text(generator.serialize(generator.build_document(comparison)))
    monkeypatch.setattr(parity, "prepare_sources", lambda *args, **kwargs: None)
    monkeypatch.setattr(parity, "run_matrix", lambda *args, **kwargs: comparison)
    assert generator.main(["--offline", "--check", "--output", str(output)]) == 0
