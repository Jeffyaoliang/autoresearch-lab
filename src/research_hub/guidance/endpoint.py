"""Bounded submit/status endpoint; caller data cannot choose executable code."""

import base64
import json
import sys

from .delivery import Delivery
from .queue import GuidanceQueue
from .rpc import binding, guidance_dir
from .runtime import validate_request


def request(data, root=None, bound=None):
    validate_request(data)
    root = guidance_dir() if root is None else root
    bound = binding() if bound is None else bound
    if data["operation"] == "submit":
        queue = GuidanceQueue(root / "inbox", bound["session_alias"])
        path = queue._path(data["request_id"])
        if not path.exists() and sum(1 for _ in (root / "inbox").glob("request_*.json")) >= 100:
            raise ValueError("pilot_queue_quota_reached")
        submitted = queue.submit(data["request_id"], data["message"])
        result = Delivery(root, None, bound).status(data["request_id"])
        result["duplicate"] = submitted["duplicate"]
        return result
    return Delivery(root, None, bound).status(data["request_id"])


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    try:
        if len(argv) != 1 or len(argv[0]) > 16000:
            raise ValueError("invalid_request")
        data = json.loads(base64.b64decode(argv[0], validate=True))
        result = {"available": True, **request(data)}
    except Exception as exc:
        codes = {
            "invalid_request",
            "invalid_request_id",
            "invalid_message",
            "possible_secret_rejected",
            "idempotency_conflict",
            "pilot_queue_quota_reached",
            "operation_not_allowed",
        }
        reason = (
            str(exc)
            if isinstance(exc, ValueError) and str(exc) in codes
            else "guidance_unavailable_or_requires_review"
        )
        result = {
            "available": False,
            "reason": reason,
            "execution_authorized": False,
            "model_processing_started": False,
        }
    print("RAH_DATA_" + base64.b64encode(json.dumps(result).encode()).decode() + "_RAH_END")


if __name__ == "__main__":
    main()
