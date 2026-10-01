"""Send each finished call to a trace collector as one OTLP span. Never raises.

This is the ledger row from `jev_meter`, in a second place: the same event,
the same fields, posted as OTLP/HTTP JSON to a local collector such as
`local-genai-traces`. Stdlib only, for the reason `jev.py` is.

**Off unless pointed somewhere.** `JEV_TRACES_URL` names the collector's
traces endpoint, for example `http://127.0.0.1:4338/v1/traces`. Unset, empty
or an off word (`0 off false no disabled`) sends nothing. The meter defaults
to on because it writes a local file; this defaults to off because it needs
a listener that most machines do not run.

**What leaves.** Only the event's fields: caller, stage, primitive, model,
outcome, cause, the numeric answer, confidence, token counts and duration.
The event never holds the question or the state, so neither can reach a span.
A text field goes only when it is an identifier (`IDENTIFIER`); anything
else, such as a path or a sentence passed to `jev record`, is dropped.

**What it costs a caller.** One POST after the answer is printed, bounded in wall clock by
`JEV_TRACES_TIMEOUT` seconds (default 0.5). A refused connection returns at
once; a collector that hangs costs the bound and no more.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import threading
import time
import urllib.request

#: The words that switch the export off, the same set the meter accepts.
OFF = ("0", "off", "false", "no", "disabled")

DEFAULT_TIMEOUT = 0.5

#: What `export` returns. Only `sent` means the collector accepted the span.
SENT = "sent"
SWITCHED_OFF = "switched off"
FAILED = "the collector did not take the span"

#: OTLP status codes.
STATUS_OK = 1
STATUS_ERROR = 2

#: OTLP span kind CLIENT: Jev calls a remote model.
KIND_CLIENT = 3

#: Event fields copied as `jev.<field>` span attributes.
FIELDS = ("caller", "stage", "arm", "pair", "question_id", "shadow",
          "primitive", "outcome", "cause", "answer", "confidence", "ordering",
          "changed", "questions")


def url_of(env) -> str:
    value = (env.get("JEV_TRACES_URL") or "").strip()
    return "" if value.lower() in OFF else value


def timeout_of(env) -> float:
    try:
        return max(0.05, float((env.get("JEV_TRACES_TIMEOUT") or "").strip()
                               or DEFAULT_TIMEOUT))
    except ValueError:
        return DEFAULT_TIMEOUT


def value_of(value) -> dict | None:
    """One OTLP AnyValue, or None for a value with no faithful form."""
    if isinstance(value, bool):
        return {"boolValue": value}
    if isinstance(value, int):
        return {"intValue": str(value)}
    if isinstance(value, float):
        return {"doubleValue": value}
    if isinstance(value, str):
        return {"stringValue": value}
    if isinstance(value, (list, tuple, dict)):
        return {"stringValue": json.dumps(value, sort_keys=True)}
    return None


#: The only shape of text a span may carry: an identifier, a model name, a
#: word. The ledger refuses a field it cannot store, but this runs whether or
#: not the ledger does, so it holds its own line: text that could be a path,
#: a subject or a sentence is dropped, never sent.
IDENTIFIER = re.compile(r"[A-Za-z0-9_.:+-]{1,128}")


def safe(value):
    """The value when a span may carry it, else None."""
    if isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return value if IDENTIFIER.fullmatch(value) else None
    if isinstance(value, (list, tuple)) and all(
            isinstance(v, (int, float)) and not isinstance(v, bool) for v in value):
        return list(value)
    return None


def attributes(pairs) -> list:
    out = []
    for key, value in pairs:
        value = safe(value)
        if value is None:
            continue
        encoded = value_of(value)
        if encoded is not None:
            out.append({"key": key, "value": encoded})
    return out


def span_of(event: dict, now_ns: int | None = None) -> dict:
    """The OTLP/JSON request body for one event."""
    end = time.time_ns() if now_ns is None else now_ns
    duration = event.get("duration_ms")
    start = end - int(duration) * 1_000_000 if isinstance(duration, int) else end
    model = event.get("model")
    provider = event.get("provider")
    tokens_in, tokens_out = event.get("tokens_in"), event.get("tokens_out")
    outcome = event.get("outcome")
    # A call that reached a model is an LLM span; a gate or a caller's own
    # report of its old path is a step in the caller's chain.
    kind = "LLM" if model or tokens_in is not None else "CHAIN"
    pairs = [
        ("openinference.span.kind", kind),
        ("llm.provider", provider),
        ("llm.model_name", model),
        ("llm.token_count.prompt", tokens_in),
        ("llm.token_count.completion", tokens_out),
        ("gen_ai.system", provider),
        ("gen_ai.operation.name", event.get("primitive")),
        ("gen_ai.request.model", model),
        ("gen_ai.usage.input_tokens", tokens_in),
        ("gen_ai.usage.output_tokens", tokens_out),
    ]
    pairs += [(f"jev.{name}", event.get(name)) for name in FIELDS]
    status = {"code": STATUS_OK}
    if outcome not in (None, "ok"):
        status = {"code": STATUS_ERROR,
                  "message": f"{safe(outcome) or 'failed'}: {safe(event.get('cause')) or 'unknown'}"}
    span = {
        "traceId": secrets.token_hex(16),
        "spanId": secrets.token_hex(8),
        "name": f"jev.{safe(event.get('primitive')) or 'call'}",
        "kind": KIND_CLIENT,
        "startTimeUnixNano": str(start),
        "endTimeUnixNano": str(end),
        "attributes": attributes(pairs),
        "status": status,
    }
    # Phoenix files spans by project; without the key they land in `default`.
    resource = attributes([("service.name", "jev"),
                           ("openinference.project.name", "jev"),
                           ("telemetry.sdk.language", "python")])
    return {"resourceSpans": [{
        "resource": {"attributes": resource},
        "scopeSpans": [{"scope": {"name": "local-jev"}, "spans": [span]}],
    }]}


def export(event: dict, env=None, opener=None) -> str:
    """Post one event as a span. Never raises; the return value is for the suite."""
    env = os.environ if env is None else env
    try:
        url = url_of(env)
        if not url:
            return SWITCHED_OFF
        body = json.dumps(span_of(event)).encode("utf-8")
        request = urllib.request.Request(
            url, data=body, method="POST",
            headers={"Content-Type": "application/json"})
        send = opener or urllib.request.urlopen
        limit = timeout_of(env)
        result = []

        def post():
            try:
                with send(request, timeout=limit) as response:
                    response.read()
                result.append(SENT)
            except Exception:
                result.append(FAILED)

        # The socket timeout bounds each blocking read, not the whole POST: a
        # collector that trickles bytes could hold a caller for as long as it
        # likes. A daemon thread joined with the bound caps the wall clock,
        # and an unfinished one dies with the process.
        worker = threading.Thread(target=post, daemon=True)
        worker.start()
        worker.join(limit)
        return result[0] if result else FAILED
    except Exception:
        # Bare `Exception`, as in `jev_meter.record`: a trace is bookkeeping,
        # and bookkeeping may never be the reason a judgment fails.
        return FAILED
