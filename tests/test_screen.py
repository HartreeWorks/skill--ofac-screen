from __future__ import annotations

import contextlib
import copy
import datetime as dt
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
import urllib.error
from unittest import mock


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "screen.py"
SPEC = importlib.util.spec_from_file_location("ofac_screen_screen", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
screen = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(screen)


def make_request(*case_ids: str) -> dict:
    return screen.make_request(
        {"cases": [{"id": case_id, "name": f"Synthetic {case_id}"} for case_id in case_ids]},
        95,
    )


def valid_sources() -> list[dict[str, str]]:
    return [
        {
            "source": "SDN",
            "publishDate": "2025-01-01T00:00:00Z",
            "downloadDate": "2025-01-02T00:00:00Z",
        },
        {
            "source": "NONSDN",
            "publishDate": "2025-01-03T00:00:00Z",
            "downloadDate": "2025-01-04T00:00:00Z",
        },
    ]


def synthetic_match(source: str = "SDN") -> dict:
    return {
        "score": 98.5,
        "sanction": {
            "id": "synthetic-record-1",
            "name": "Synthetic Listed Subject",
            "source": source,
            "aliases": ["Synthetic Alias"],
        },
    }


def valid_response(request: dict, matches_by_id: dict[str, list[dict]] | None = None) -> dict:
    matches_by_id = matches_by_id or {}
    results = []
    for case in request["cases"]:
        matches = copy.deepcopy(matches_by_id.get(case["id"], []))
        results.append({
            "id": case["id"],
            "name": case["name"],
            "matches": matches,
            "matchCount": len(matches),
        })
    return {"error": False, "sources": valid_sources(), "results": results}


def one_run_dir(output_dir: Path) -> Path:
    run_dirs = [entry for entry in output_dir.iterdir() if entry.is_dir()]
    if len(run_dirs) != 1:
        raise AssertionError(f"Expected one run directory, found {run_dirs!r}")
    return run_dirs[0]


class RequestValidationTests(unittest.TestCase):
    def test_accepts_supported_optional_fields_and_preserves_them(self) -> None:
        data = {
            "cases": [
                {
                    "id": "person-1",
                    "name": "Synthetic Person",
                    "type": "person",
                    "dob": "1980-01-02",
                    "citizenship": "United Kingdom",
                    "nationality": "France",
                    "address": {"city": "Example City", "country": "France"},
                    "identification": [{"idNumber": "ABC-123", "type": "passport", "country": "France"}],
                }
            ]
        }

        request = screen.make_request(data, 95)

        self.assertEqual(request["sources"], ["SDN", "NONSDN"])
        self.assertEqual(request["minScore"], 95)
        self.assertEqual(request["cases"], data["cases"])

    def test_rejects_invalid_input_fields_and_dates(self) -> None:
        invalid_inputs = [
            (
                {"cases": [{"name": "Synthetic", "unexpected": "field"}]},
                "Unrecognised input field",
            ),
            (
                {"cases": [{"name": "Synthetic", "type": "company"}]},
                "Type must be person or organization",
            ),
            (
                {"cases": [{"name": "Synthetic", "dob": "2000-02-30"}]},
                "DOB is not a valid date",
            ),
            (
                {"cases": [{"name": "Synthetic", "dob": "01/02/2000"}]},
                "DOB must use YYYY-MM-DD",
            ),
            (
                {"cases": [{"name": "Synthetic", "dob": "2999-01-01"}]},
                "DOB cannot be in the future",
            ),
            (
                {"cases": [{"name": ""}]},
                "Fields must be non-empty strings",
            ),
            (
                {"cases": [{"name": "Synthetic", "address": {"unknown": "field"}}]},
                "Unrecognised input field",
            ),
            (
                {"cases": [{"name": "Synthetic", "identification": [{"type": "passport"}]}]},
                "Required input field is missing",
            ),
        ]
        for data, message in invalid_inputs:
            with self.subTest(data=data):
                with self.assertRaisesRegex(screen.ScreeningError, message):
                    screen.make_request(data, 95)

    def test_rejects_invalid_minimum_scores(self) -> None:
        for score in (79, 101, True, 95.0):
            with self.subTest(score=score):
                with self.assertRaisesRegex(screen.ScreeningError, "Minimum score"):
                    screen.make_request({"cases": [{"name": "Synthetic"}]}, score)

    def test_generated_case_ids_cannot_collide_with_supplied_ids(self) -> None:
        data = {"cases": [{"id": "case-2", "name": "First"}, {"name": "Second"}]}

        with self.assertRaisesRegex(screen.ScreeningError, "Case IDs must be unique"):
            screen.make_request(data, 95)


class ClassificationTests(unittest.TestCase):
    def test_reordered_results_are_mapped_to_the_original_case_order(self) -> None:
        request = make_request("first", "second")
        response = valid_response(request, {"second": [synthetic_match()]})
        response["results"].reverse()

        summary_cases = screen.classify(request, response)

        self.assertEqual([case["id"] for case in summary_cases], ["first", "second"])
        self.assertEqual([case["status"] for case in summary_cases], ["no_potential_matches", "potential_matches"])
        self.assertEqual([case["match_count"] for case in summary_cases], [0, 1])

    def test_duplicate_missing_and_foreign_result_ids_fail_closed(self) -> None:
        request = make_request("first", "second")
        scenarios = {
            "duplicate": ("Duplicate case result ID", lambda results: results[1].update(id="first")),
            "missing": ("A result has no valid case ID", lambda results: results[1].pop("id")),
            "foreign": ("Response case IDs do not match the request", lambda results: results[1].update(id="foreign")),
        }

        for name, (message, mutate) in scenarios.items():
            with self.subTest(name=name):
                response = valid_response(request)
                mutate(response["results"])
                with self.assertRaisesRegex(screen.ScreeningError, message):
                    screen.classify(request, response)

    def test_malformed_response_and_provider_error_are_rejected(self) -> None:
        request = make_request("first")

        with self.assertRaisesRegex(screen.ScreeningError, "API response is not an object"):
            screen.classify(request, [])

        for response in (
            {"sources": valid_sources(), "results": []},
            {"error": True, "sources": valid_sources(), "results": []},
            {"error": False, "errorMessage": "synthetic failure", "sources": valid_sources(), "results": []},
        ):
            with self.subTest(response=response):
                with self.assertRaises(screen.ScreeningError):
                    screen.classify(request, response)

    def test_missing_sources_dates_and_matches_are_rejected(self) -> None:
        request = make_request("first")
        scenarios = {
            "sources missing": ("Incomplete source coverage", lambda response: response.pop("sources")),
            "source missing": ("Incomplete source coverage", lambda response: response["sources"].pop()),
            "unexpected source": (
                "Source coverage does not match",
                lambda response: response["sources"][0].update(source="OTHER"),
            ),
            "date missing": (
                "Missing or invalid source dates",
                lambda response: response["sources"][0].pop("publishDate"),
            ),
            "date malformed": (
                "Missing or invalid source dates",
                lambda response: response["sources"][0].update(downloadDate="not-a-date"),
            ),
            "source error flag": (
                "requested source reported an error",
                lambda response: response["sources"][0].update(error=True),
            ),
            "source error message": (
                "requested source reported an error",
                lambda response: response["sources"][0].update(errorMessage="synthetic source failure"),
            ),
            "matches missing": (
                "A result is missing its matches array",
                lambda response: (response["results"][0].pop("matches"), response["results"][0].update(matchCount=1)),
            ),
        }

        for name, (message, mutate) in scenarios.items():
            with self.subTest(name=name):
                response = valid_response(request)
                mutate(response)
                with self.assertRaisesRegex(screen.ScreeningError, message):
                    screen.classify(request, response)

    def test_live_zero_count_shape_omits_matches(self) -> None:
        request = make_request("first")
        for omitted in (True, False):
            response = valid_response(request)
            if omitted:
                response["results"][0].pop("matches")
            else:
                response["results"][0]["matches"] = None
            self.assertEqual(screen.classify(request, response)[0]["status"], "no_potential_matches")
            with tempfile.TemporaryDirectory() as temporary:
                summary = screen.run(request, "synthetic-key", Path(temporary), transport=lambda *_: (200, json.dumps(response)))
                self.assertEqual(summary["status"], "no_potential_matches")
                self.assertIn("No potential matches returned", Path(summary["report"]).read_text())

    def test_vendor_formatted_name_is_exposed_and_id_remains_authoritative(self) -> None:
        request = make_request("first")
        response = valid_response(request)
        response["results"][0]["name"] = "SYNTHETIC FIRST"
        case = screen.classify(request, response)[0]
        self.assertEqual(case["name"], "Synthetic first")
        self.assertEqual(case["api_name"], "SYNTHETIC FIRST")

    def test_each_result_requires_a_name_and_consistent_integer_match_count(self) -> None:
        request = make_request("first")
        scenarios = {
            "name missing": ("result lacks the screened name", lambda result: result.pop("name")),
            "name blank": ("result lacks the screened name", lambda result: result.update(name="  ")),
            "match count missing": (
                "Match count is missing or inconsistent",
                lambda result: result.pop("matchCount"),
            ),
            "match count bool": (
                "Match count is missing or inconsistent",
                lambda result: result.update(matchCount=False),
            ),
        }

        for name, (message, mutate) in scenarios.items():
            with self.subTest(name=name):
                response = valid_response(request)
                mutate(response["results"][0])
                with self.assertRaisesRegex(screen.ScreeningError, message):
                    screen.classify(request, response)

    def test_match_count_must_equal_the_returned_matches(self) -> None:
        request = make_request("first")
        response = valid_response(request, {"first": [synthetic_match()]})
        response["results"][0]["matchCount"] = 0

        with self.assertRaisesRegex(screen.ScreeningError, "Match count is missing or inconsistent"):
            screen.classify(request, response)

    def test_malformed_match_records_are_rejected(self) -> None:
        request = make_request("first")
        mutations = {
            "non-finite score": lambda match: match.update(score=float("nan")),
            "missing sanction": lambda match: match.pop("sanction"),
            "unexpected source": lambda match: match["sanction"].update(source="OTHER"),
        }

        for name, mutate in mutations.items():
            with self.subTest(name=name):
                response = valid_response(request, {"first": [synthetic_match()]})
                mutate(response["results"][0]["matches"][0])
                with self.assertRaises(screen.ScreeningError):
                    screen.classify(request, response)


class RunAndEvidenceTests(unittest.TestCase):
    def test_success_retains_request_response_summary_and_candidate_report(self) -> None:
        request = make_request("first", "second")
        response = valid_response(request, {"second": [synthetic_match()]})

        with tempfile.TemporaryDirectory() as temporary:
            output_dir = Path(temporary) / "runs"
            summary = screen.run(request, "synthetic-api-key", output_dir, transport=lambda _request, _key: (200, json.dumps(response)))
            run_dir = one_run_dir(output_dir)

            self.assertEqual(summary["status"], "potential_matches")
            self.assertEqual(summary["cases"][0]["status"], "no_potential_matches")
            self.assertEqual(summary["cases"][1]["status"], "potential_matches")
            self.assertEqual(json.loads((run_dir / "request.json").read_text(encoding="utf-8")), request)
            self.assertEqual(json.loads((run_dir / "response.json").read_text(encoding="utf-8")), response)
            self.assertEqual(json.loads((run_dir / "summary.json").read_text(encoding="utf-8")), summary)
            report = (run_dir / "report.md").read_text(encoding="utf-8")
            self.assertIn("Synthetic Listed Subject", report)
            self.assertIn("No potential matches returned", report)
            self.assertIn("Potential matches—review needed", report)
            for filename in ("request.json", "response.json", "summary.json", "report.md"):
                self.assertEqual((run_dir / filename).stat().st_mode & 0o777, 0o600)

    def test_http_200_provider_error_is_screening_failure_and_is_retained(self) -> None:
        request = make_request("first")
        response = {"error": True, "errorMessage": "synthetic provider failure"}

        with tempfile.TemporaryDirectory() as temporary:
            output_dir = Path(temporary) / "runs"
            summary = screen.run(request, "synthetic-api-key", output_dir, transport=lambda _request, _key: (200, json.dumps(response)))
            run_dir = one_run_dir(output_dir)

            self.assertEqual(summary["status"], "screening_failed")
            self.assertEqual(summary["http_status"], 200)
            self.assertIn("API error flag", summary["error"])
            self.assertEqual(json.loads((run_dir / "response.json").read_text(encoding="utf-8")), response)
            self.assertIn("Response was not validated", (run_dir / "report.md").read_text(encoding="utf-8"))

    def test_http_error_status_is_retained_without_retry(self) -> None:
        request = make_request("first")
        response = valid_response(request)
        calls = 0

        def transport(_request: dict, _key: str) -> tuple[int, str]:
            nonlocal calls
            calls += 1
            return 503, json.dumps(response)

        with tempfile.TemporaryDirectory() as temporary:
            summary = screen.run(request, "synthetic-api-key", Path(temporary), transport=transport)

            self.assertEqual(calls, 1)
            self.assertEqual(summary["status"], "screening_failed")
            self.assertIn("HTTP 503", summary["error"])

    def test_invalid_json_response_is_retained_without_credentials(self) -> None:
        request = make_request("first")
        key = "synthetic-api-key"

        with tempfile.TemporaryDirectory() as temporary:
            output_dir = Path(temporary)
            summary = screen.run(request, key, output_dir, transport=lambda _request, _key: (200, f"not-json {key}"))
            run_dir = one_run_dir(output_dir)

            self.assertEqual(summary["status"], "screening_failed")
            self.assertIn("invalid JSON", summary["error"])
            self.assertFalse((run_dir / "response.json").exists())
            self.assertEqual((run_dir / "response.txt").read_text(encoding="utf-8"), "not-json [REDACTED]")
            self.assertNotIn(key, "".join(path.read_text(encoding="utf-8") for path in run_dir.iterdir()))

    def test_credentials_are_redacted_from_all_retained_evidence(self) -> None:
        request = make_request("first")
        key = "synthetic-api-key"
        bodies = {
            "json": (500, json.dumps({"error": True, "message": f"echoed {key}"})),
            "text": (500, f"provider said {key}"),
        }

        with tempfile.TemporaryDirectory() as temporary:
            for name, (status, body) in bodies.items():
                with self.subTest(name=name):
                    output_dir = Path(temporary) / name
                    summary = screen.run(request, key, output_dir, transport=lambda _request, _key, status=status, body=body: (status, body))
                    run_dir = one_run_dir(output_dir)
                    retained = "".join(path.read_text(encoding="utf-8") for path in run_dir.iterdir())
                    self.assertNotIn(key, retained)
                    self.assertIn("[REDACTED]", retained)
                    self.assertEqual(summary["status"], "screening_failed")

    def test_network_errors_fail_closed_without_exposing_transport_details(self) -> None:
        request = make_request("first")

        def transport(_request: dict, _key: str) -> tuple[int, str]:
            raise urllib.error.URLError("synthetic network outage")

        with tempfile.TemporaryDirectory() as temporary:
            output_dir = Path(temporary)
            summary = screen.run(request, "synthetic-api-key", output_dir, transport=transport)
            run_dir = one_run_dir(output_dir)

            self.assertEqual(summary["status"], "screening_failed")
            self.assertNotIn("http_status", summary)
            self.assertIn("Transport or evidence-write failure", summary["error"])
            self.assertNotIn("synthetic network outage", (run_dir / "summary.json").read_text(encoding="utf-8"))
            self.assertFalse((run_dir / "response.json").exists())


class NetworkAndCliTests(unittest.TestCase):
    def test_redirect_handler_refuses_forwarding(self) -> None:
        with self.assertRaisesRegex(screen.ScreeningError, "API redirect refused"):
            screen.NoRedirect().redirect_request(None, None, 302, "Found", {}, "https://example.invalid")

    def test_dry_run_validates_and_does_not_open_a_network_connection(self) -> None:
        data = {"cases": [{"name": "Synthetic", "type": "organization"}]}

        with tempfile.TemporaryDirectory() as temporary:
            input_path = Path(temporary) / "cases.json"
            input_path.write_text(json.dumps(data), encoding="utf-8")
            previous_umask = os.umask(0)
            os.umask(previous_umask)
            try:
                with (
                    mock.patch.object(screen.sys, "argv", [str(SCRIPT), "--input", str(input_path), "--dry-run"]),
                    mock.patch.object(screen.urllib.request, "build_opener", side_effect=AssertionError("network access during dry-run")) as build_opener,
                    contextlib.redirect_stdout(io.StringIO()) as stdout,
                ):
                    return_code = screen.main()
            finally:
                os.umask(previous_umask)

            payload = json.loads(stdout.getvalue())
            self.assertEqual(return_code, 0)
            self.assertFalse(build_opener.called)
            self.assertTrue(payload["dry_run"])
            self.assertEqual(payload["case_count"], 1)
            self.assertEqual(payload["request"]["sources"], ["SDN", "NONSDN"])


if __name__ == "__main__":
    unittest.main(verbosity=1)
