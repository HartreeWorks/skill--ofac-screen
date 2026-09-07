#!/usr/bin/env python3
"""On-demand OFAC-API.com screening with local evidence and fail-closed results."""

import argparse
import datetime as dt
import json
import math
import os
from pathlib import Path
import re
import sys
import tempfile
import urllib.error
import urllib.request


ENDPOINT = "https://api.ofac-api.com/v4/screen"
SOURCES = ["SDN", "NONSDN"]
MAX_BYTES = 20 * 1024 * 1024
STATUSES = {
    "no_potential_matches": "No potential matches returned",
    "potential_matches": "Potential matches—review needed",
    "screening_failed": "Screening failed",
}


class ScreeningError(ValueError):
    """An input or response cannot support a completed screening."""


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ScreeningError(message)


def text_fields(obj: dict, allowed: set[str], required: set[str]) -> None:
    require(isinstance(obj, dict), "Expected an object.")
    require(not set(obj) - allowed, "Unrecognised input field; check the skill schema.")
    require(required <= set(obj), "Required input field is missing.")
    for value in obj.values():
        require(isinstance(value, str) and bool(value.strip()), "Fields must be non-empty strings.")
        require(len(value) <= 1000 and not any(ord(c) < 32 for c in value), "Invalid field length or control character.")


def make_request(data: dict, min_score: int) -> dict:
    require(type(min_score) is int and 80 <= min_score <= 100, "Minimum score must be an integer from 80 to 100.")
    require(isinstance(data, dict) and set(data) == {"cases"}, "Input must contain only a cases array.")
    require(isinstance(data["cases"], list) and 1 <= len(data["cases"]) <= 500, "Supply 1–500 cases.")
    cases = []
    ids = set()
    fields = {"id", "name", "type", "dob", "citizenship", "nationality"}
    for index, original in enumerate(data["cases"], 1):
        require(isinstance(original, dict), "Each case must be an object.")
        case = dict(original)
        address = case.pop("address", None)
        identification = case.pop("identification", None)
        text_fields(case, fields, {"name"})
        require("type" not in case or case["type"] in {"person", "organization"}, "Type must be person or organization.")
        if "dob" in case:
            require(bool(re.fullmatch(r"\d{4}-\d{2}-\d{2}", case["dob"])), "DOB must use YYYY-MM-DD.")
            try:
                date = dt.date.fromisoformat(case["dob"])
            except ValueError:
                raise ScreeningError("DOB is not a valid date.") from None
            require(date <= dt.date.today(), "DOB cannot be in the future.")
        if "address" in original:
            text_fields(address, {"address1", "address2", "city", "stateOrProvince", "postalCode", "country"}, set())
            require(bool(address), "Address must contain at least one field.")
            case["address"] = address
        if "identification" in original:
            require(isinstance(identification, list) and 1 <= len(identification) <= 20, "Supply 1–20 identification records.")
            for identifier in identification:
                text_fields(identifier, {"idNumber", "type", "country"}, {"idNumber"})
            case["identification"] = identification
        case.setdefault("id", f"case-{index}")
        require(case["id"] not in ids, "Case IDs must be unique, including generated IDs.")
        ids.add(case["id"])
        cases.append(case)
    return {"sources": SOURCES.copy(), "minScore": min_score, "cases": cases}


def valid_date(value: object) -> bool:
    if not isinstance(value, str) or not value.strip():
        return False
    try:
        dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        return True
    except ValueError:
        return False


def result_matches(result: dict) -> list:
    matches = result.get("matches")
    # The live v4 API omits matches for a successful zero-count result.
    if matches is None and type(result.get("matchCount")) is int and result["matchCount"] == 0:
        return []
    require(isinstance(matches, list), "A result is missing its matches array.")
    return matches


def classify(request: dict, response: dict) -> list[dict]:
    require(isinstance(response, dict), "API response is not an object.")
    require(response.get("error") is False, "API error flag is set or missing; inspect the saved response.")
    require(not response.get("errorMessage") and not response.get("errorMessages"), "API reported an error message; inspect the saved response.")
    sources = response.get("sources")
    require(isinstance(sources, list) and len(sources) == len(SOURCES), "Incomplete source coverage.")
    require(all(isinstance(s, dict) and isinstance(s.get("source"), str) for s in sources), "Malformed source metadata.")
    require({s["source"].upper() for s in sources} == set(SOURCES), "Source coverage does not match the requested OFAC lists.")
    for source in sources:
        require(not source.get("error") and not source.get("errorMessage") and not source.get("errorMessages"), "A requested source reported an error.")
        require(valid_date(source.get("publishDate")) and valid_date(source.get("downloadDate")), "Missing or invalid source dates.")
    results = response.get("results")
    require(isinstance(results, list) and len(results) == len(request["cases"]), "Missing or extra case results.")
    by_id = {}
    for result in results:
        require(isinstance(result, dict) and isinstance(result.get("id"), str), "A result has no valid case ID.")
        require(result["id"] not in by_id, "Duplicate case result ID.")
        require(isinstance(result.get("name"), str) and bool(result["name"].strip()), "A result lacks the screened name.")
        require(not result.get("error") and not result.get("errorMessage") and not result.get("errorMessages"), "A case reported an error.")
        matches = result_matches(result)
        require(type(result.get("matchCount")) is int and result["matchCount"] == len(matches), "Match count is missing or inconsistent; results may be truncated.")
        for match in matches:
            require(isinstance(match, dict), "Malformed match.")
            score = match.get("score")
            require(type(score) in (int, float) and math.isfinite(score) and 0 <= score <= 100, "Invalid match score.")
            sanction = match.get("sanction")
            require(isinstance(sanction, dict) and isinstance(sanction.get("name"), str) and bool(sanction["name"].strip()), "Match lacks a sanction name.")
            require(isinstance(sanction.get("source"), str) and sanction["source"].upper() in SOURCES, "Match has an unexpected source.")
        by_id[result["id"]] = result
    require(set(by_id) == {case["id"] for case in request["cases"]}, "Response case IDs do not match the request.")
    return [{"id": case["id"], "name": case["name"], "api_name": by_id[case["id"]]["name"],
             "status": "potential_matches" if result_matches(by_id[case["id"]]) else "no_potential_matches",
             "match_count": len(result_matches(by_id[case["id"]]))} for case in request["cases"]]


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ScreeningError("API redirect refused; request was not forwarded.")


def post(request: dict, key: str) -> tuple[int, str]:
    http_request = urllib.request.Request(
        ENDPOINT, data=json.dumps(request).encode("utf-8"), method="POST",
        headers={"Content-Type": "application/json", "Accept": "application/json", "apiKey": key},
    )
    opener = urllib.request.build_opener(NoRedirect())
    try:
        connection = opener.open(http_request, timeout=45)
    except urllib.error.HTTPError as exc:
        connection = exc
    with connection:
        body = connection.read(MAX_BYTES + 1)
        require(len(body) <= MAX_BYTES, "API response exceeded the size limit.")
        return connection.code, body.decode("utf-8", errors="replace")


def write_json(path: Path, value: object) -> None:
    write_private(path, json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False) + "\n")


def write_private(path: Path, value: str) -> None:
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w", encoding="utf-8") as output:
        output.write(value)


def md(value: object) -> str:
    text = str(value).replace("\n", " ").replace("\r", " ")
    for char in "\\`*_{}[]<>()#!|":
        text = text.replace(char, "\\" + char)
    return text


def report(summary: dict, request: dict, response: object) -> str:
    lines = ["# OFAC screening report", "", f"Status: {STATUSES[summary['status']]}", "",
             f"Started: {summary['started_at']}", f"Completed: {summary['completed_at']}", "",
             "Provider: OFAC-API.com (commercial service)",
             f"Requested lists: SDN, NONSDN. Minimum score: {request['minScore']} (prototype setting).", "",
             "This records a list-screening result, not payment approval. Potential matches require human review.", ""]
    if summary.get("error"):
        lines.extend(["Failure: " + md(summary["error"]), ""])
    lines.extend(["| Case | Name supplied | Result | Matches |", "| --- | --- | --- | --- |"])
    for case in summary["cases"]:
        lines.append(f"| {md(case['id'])} | {md(case['name'])} | {STATUSES[case['status']]} | {case.get('match_count', 'Unknown')} |")
    lines.extend(["", "## Vendor list metadata", "", "Dates below are vendor-supplied; they do not independently prove data freshness.", ""])
    if isinstance(response, dict) and isinstance(response.get("sources"), list):
        lines.extend(["| List | Publication date | Download date |", "| --- | --- | --- |"])
        for source in response["sources"]:
            if isinstance(source, dict):
                lines.append(f"| {md(source.get('source', 'Missing'))} | {md(source.get('publishDate', 'Missing'))} | {md(source.get('downloadDate', 'Missing'))} |")
    else:
        lines.append("Unavailable.")
    lines.extend(["", "## Returned candidates", ""])
    if summary["status"] == "screening_failed":
        lines.append("Response was not validated. Inspect the saved response for any partial evidence; no negative result is established.")
    elif isinstance(response, dict):
        by_id = {result["id"]: result for result in response["results"]}
        for case in request["cases"]:
            lines.extend(["", "### " + md(case["name"]) + " (" + md(case["id"]) + ")", ""])
            result = by_id[case["id"]]
            if result["name"] != case["name"]:
                lines.extend(["Vendor returned name: " + md(result["name"]) + ". Results are mapped by case ID.", ""])
            matches = result_matches(result)
            if not matches:
                lines.append("No potential matches returned at these settings.")
                continue
            lines.extend(["| Candidate | List | Similarity score | Vendor record ID |", "| --- | --- | --- | --- |"])
            for match in matches:
                sanction = match["sanction"]
                lines.append(f"| {md(sanction['name'])} | {md(sanction['source'])} | {match['score']} | {md(sanction.get('id', 'Not supplied'))} |")
            lines.append("\nCompare the complete identifiers, aliases, match explanations and source links in response.json before any disposition.")
    lines.extend(["", "## Evidence", "", "The adjacent request.json holds supplied identifiers and settings. response.json holds the complete parsed vendor response, including all returned candidate details. response.txt is retained if the body was not JSON. Credentials are excluded/redacted.", "",
                  "No human disposition has been recorded. These local files are editable; they are not a tamper-proof audit system.", ""])
    return "\n".join(lines)


def run(request: dict, key: str, output_dir: Path, transport=post) -> dict:
    output_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    run_dir = Path(tempfile.mkdtemp(prefix=dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ-"), dir=output_dir))
    started = dt.datetime.now(dt.timezone.utc).isoformat()
    write_json(run_dir / "request.json", request)
    summary = {"status": "screening_failed", "started_at": started, "endpoint": ENDPOINT,
               "sources": SOURCES.copy(), "min_score": request["minScore"],
               "report": str(run_dir / "report.md"),
               "cases": [{"id": c["id"], "name": c["name"], "status": "screening_failed"} for c in request["cases"]]}
    response = None
    try:
        status, body = transport(request, key)
        summary["http_status"] = status
        # A misbehaving server might echo credentials in an error body.
        body = body.replace(key, "[REDACTED]")
        try:
            response = json.loads(body)
        except (ValueError, RecursionError):
            write_private(run_dir / "response.txt", body)
            raise ScreeningError("API returned invalid JSON.") from None
        write_json(run_dir / "response.json", response)
        require(status == 200, f"API returned HTTP {status}; inspect the saved response. No automatic retry was made.")
        cases = classify(request, response)
        summary["cases"] = cases
        summary["status"] = "potential_matches" if any(c["status"] == "potential_matches" for c in cases) else "no_potential_matches"
    except ScreeningError as exc:
        summary["error"] = str(exc)
    except (OSError, ValueError, RecursionError):
        summary["error"] = "Transport or evidence-write failure; request may have consumed checks. No automatic retry was made."
    summary["completed_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
    write_json(run_dir / "summary.json", summary)
    write_private(run_dir / "report.md", report(summary, request, response))
    return summary


def main() -> int:
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path, help="JSON object containing cases; see SKILL.md")
    parser.add_argument("--min-score", type=int, default=95, help="80–100; prototype default: vendor-recommended 95")
    parser.add_argument("--dry-run", action="store_true", help="Validate and print request without credentials or network access")
    data_root = Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local/share")))
    parser.add_argument("--output-dir", type=Path, default=data_root / "ofac-screen/runs", help="Parent directory for private evidence folders")
    args = parser.parse_args()
    try:
        require(args.input.stat().st_size <= 2 * 1024 * 1024, "Input file is too large.")
        request = make_request(json.loads(args.input.read_text(encoding="utf-8")), args.min_score)
        if args.dry_run:
            print(json.dumps({"dry_run": True, "case_count": len(request["cases"]), "request": request}, ensure_ascii=False, indent=2))
            return 0
        key = os.environ.get("OFAC_API_KEY", "").strip()
        require(bool(key) and not key.startswith("op://"), "Set OFAC_API_KEY via the environment or your secret manager before live screening.")
        summary = run(request, key, args.output_dir.expanduser().resolve())
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 2 if summary["status"] == "screening_failed" else 0
    except (OSError, ValueError, RecursionError) as exc:
        message = str(exc) if isinstance(exc, ScreeningError) else "Could not read input or save evidence. Screening is unresolved."
        print(json.dumps({"status": "screening_failed", "error": message}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
