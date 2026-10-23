"""Scoped lifecycle reducer. Detection AFTER a tool event is NOT prevention.
No live subscription is opened while the channel is disabled.
"""

from datetime import datetime, timezone
from hashlib import sha256
from .policy import EXPECTED_REPLY, spec


def now():
    return datetime.now(timezone.utc).isoformat()


class EventCapture:
    def __init__(self, turn_id, session_id):
        if not isinstance(turn_id, str) or not turn_id or len(turn_id) > 100:
            raise ValueError("invalid_turn_id")
        spec(session_id)
        self.session_id = session_id
        self.turn_id = turn_id
        self.events = []
        self.text = ""
        self.seen_items = set()
        self.reply_source = None
        self.result = {
            "turn_id": turn_id,
            "state": "waiting_for_started",
            "started_observed_at": None,
            "ended_observed_at": None,
            "model_reply": None,
            "error": None,
            "forbidden_item_observed": False,
            "terminal_event_seen": False,
        }

    def feed(self, event):
        if self.result["terminal_event_seen"]:
            raise ValueError("event_after_terminal")
        if not isinstance(event, dict) or "id" in event:
            raise ValueError("not_notification")
        p = event.get("params", {})
        m = event.get("method")
        if not isinstance(p, dict) or p.get("threadId") != self.session_id:
            raise ValueError("wrong_or_missing_thread")
        turn = p.get("turn", {})
        tid = p.get("turnId", turn.get("id") if isinstance(turn, dict) else None)
        if tid != self.turn_id:
            raise ValueError("wrong_or_missing_turn")
        at = now()
        if m == "turn/started":
            if self.result["state"] != "waiting_for_started":
                raise ValueError("duplicate_start")
            self.result.update(state="inProgress", started_observed_at=at)
        elif m == "item/agentMessage/delta":
            if self.result["state"] != "inProgress":
                raise ValueError("delta_without_start")
            delta = p.get("delta")
            if not isinstance(delta, str) or len(self.text.encode()) + len(delta.encode()) > 8192:
                raise ValueError("invalid_or_oversized_reply")
            self.text += delta
            self.reply_source = "delta_stream"
        elif m in ["item/started", "item/completed"]:
            if self.result["state"] != "inProgress":
                raise ValueError("item_without_start")
            item = p.get("item", {})
            kind = item.get("type")
            if kind not in ["userMessage", "agentMessage", "reasoning"]:
                # A safety violation record, never an assurance that execution was stopped.
                self.result.update(
                    forbidden_item_observed=True,
                    error={"kind": "unexpected_item_type_detected", "raw_details_exported": False},
                )
            if m == "item/completed" and kind == "agentMessage":
                text = item.get("text", "")
                if not isinstance(text, str) or len(text.encode()) > 8192:
                    raise ValueError("invalid_reply")
                if self.reply_source != "delta_stream":
                    self.text = text
                    self.reply_source = "completed_agent_item"
                elif text and text != self.text:
                    raise ValueError("delta_final_mismatch")
        elif m == "turn/completed":
            if self.result["state"] != "inProgress":
                raise ValueError("completed_without_start")
            status = turn.get("status")
            if status not in ["completed", "failed", "interrupted"]:
                raise ValueError("invalid_final_status")
            if turn.get("error"):
                raw = repr(turn["error"]).encode()
                self.result["error"] = {
                    "kind": "server_turn_error",
                    "sha256": sha256(raw).hexdigest(),
                    "raw_details_exported": False,
                }
            self.result.update(state=status, ended_observed_at=at, terminal_event_seen=True)
            if self.text == EXPECTED_REPLY:
                self.result["model_reply"] = self.text
            elif self.text:
                self.result.update(
                    reply_redacted=True,
                    reply_sha256=sha256(self.text.encode()).hexdigest(),
                    reply_bytes=len(self.text.encode()),
                )
        else:
            raise ValueError("event_not_supported")
        self.events.append(
            {"method": m, "thread_id": self.session_id, "turn_id": self.turn_id, "observed_at": at}
        )
        if len(self.events) > 256:
            raise ValueError("event_limit_exceeded")
        return dict(self.result)

    def summary(self):
        return {
            **self.result,
            "events": list(self.events),
            "timestamps_are_client_observations": True,
            "tool_prevention_proven_by_events": False,
        }


def collect_notifications(client, turn_id, deadline_monotonic, session_id):
    """Only for a future authorized transport; never constructs or starts a turn."""
    import time

    capture = EventCapture(turn_id, session_id)
    while time.monotonic() < deadline_monotonic and not capture.result["terminal_event_seen"]:
        try:
            event = client.receive_notification(max(0.01, deadline_monotonic - time.monotonic()))
            capture.feed(event)
        except (TimeoutError, EOFError, OSError) as exc:
            capture.result["error"] = {
                "kind": "event_transport_error",
                "error_class": type(exc).__name__,
                "execution_status_unknown": True,
            }
            break
        except (ValueError, TypeError, KeyError) as exc:
            capture.result["error"] = {
                "kind": "invalid_event_stream",
                "error_class": type(exc).__name__,
                "execution_status_unknown": True,
            }
            break
    if not capture.result["terminal_event_seen"] and not capture.result["error"]:
        capture.result["error"] = {"kind": "observation_timeout", "execution_status_unknown": True}
    return capture.summary()
