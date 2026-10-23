import json
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker


def test_documented_synthetic_trace_matches_existing_schema():
    root = Path(__file__).resolve().parents[1]
    schema = json.loads((root / "schemas/trace.schema.json").read_text())
    trace = json.loads((root / "examples/trace.example.json").read_text())
    Draft202012Validator(schema, format_checker=FormatChecker()).validate(trace)
