"""The comparison arms: ask a local Kev and one Haiku transport the request
that just went to Jev, and write down how each answered.

`jev.py` starts this as a detached child process before its own request goes
out, and never waits for it. The caller's answer, exit code and wait are
Jev's alone; see `docs/work/2026-10-01-kev-and-haiku-arms/design.md` for why a
detached child and not a bounded wait.

**It only records.** No answer from here reaches a caller. Each arm writes one
`judgment` row through `jev_meter`, with the Jev row's pair id, and the report
`sd-db.sh judgments compare` pairs them up.

**It sends what Jev was sent.** The request file holds the payload after
`jev.py` redacted it and refused what it refuses. The Kev arm stays on this
machine. The Haiku arm sends the same state to a second third party: the
README says so, and `JEV_COMPARE_HAIKU_VIA=off` stops it.

**Nothing may depend on it.** Every failure here is a row with a decline
reason, or nothing at all. Stdlib only, like `jev.py`.

Run as `python3 jev_compare.py -` with the request on stdin, as `jev.py`
starts it, or as `python3 jev_compare.py REQUEST_FILE`, deleted on read.
"""

from __future__ import annotations

import json
import math
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import jev  # noqa: E402

try:
    import jev_meter  # noqa: E402
except ImportError:                          # pragma: no cover - a copy alone
    jev_meter = None

#: Seconds each arm may take before it is recorded as a timeout.
DEFAULT_TIMEOUT = 60.0

DEFAULT_KEV_URL = "http://127.0.0.1:8009/v1/systemone"
#: The checkpoint `local-kev` serves by default, as its row names it.
DEFAULT_KEV_MODEL = "jaredpalmer/kev-4b@v1.0"
#: Kev's server answers to `kev-latest` and `jev-latest` only, so a pinned
#: `JEV_MODEL` such as `jev-1.13.0` is replaced rather than refused there.
KEV_REQUEST_MODEL = "kev-latest"

#: Haiku 4.5's list price, US dollars per million tokens, for a transport
#: that does not report its own cost. A transport whose model is not Haiku
#: has no default (`"usd": None` below), and neither has a configured
#: `JEV_COMPARE_HAIKU_MODEL` other than the transport's own: its cost needs
#: the configured prices.
DEFAULT_USD_IN = 1.0
DEFAULT_USD_OUT = 5.0

#: Haiku 4.5's largest output, in tokens. A reply that could need more is
#: declined before the paid call rather than cut off after it.
MAX_OUTPUT_TOKENS = 64000
#: Room beyond the reply itself: whitespace and a code fence.
OUTPUT_SLACK = 256
#: The Haiku arm asks one request per question, concurrently. A larger batch
#: is declined before any call: no thread pile-up, no fan-out of paid calls.
MAX_HAIKU_QUESTIONS = 8
#: The most options a question may have: Kev's API limit, and the most
#: values the ledger's `probabilities` takes. A larger question is declined
#: before the paid call, whose row the ledger would refuse.
MAX_OPTIONS = 255

#: The transports, their endpoints, the variables holding their keys (the
#: first set one wins), and their model names for Claude Haiku 4.5.
#: `ANTHROPIC_API_KEY` is absent on purpose: the operator's profile unsets it,
#: and an arm that read it would start sending the day somebody exported it.
TRANSPORTS = {
    "anthropic": {
        "url": ("JEV_COMPARE_ANTHROPIC_URL", "https://api.anthropic.com/v1/messages"),
        "keys": ("JEV_COMPARE_ANTHROPIC_KEY",),
        "model": "claude-haiku-4-5",
        "usd": (DEFAULT_USD_IN, DEFAULT_USD_OUT),
    },
    "openrouter": {
        "url": ("JEV_COMPARE_OPENROUTER_URL",
                "https://openrouter.ai/api/v1/chat/completions"),
        "keys": ("JEV_COMPARE_OPENROUTER_KEY", "OPENROUTER_API_KEY"),
        "model": "anthropic/claude-haiku-4.5",
        "usd": (DEFAULT_USD_IN, DEFAULT_USD_OUT),
    },
    "baseten": {
        "url": ("JEV_COMPARE_BASETEN_URL",
                "https://inference.baseten.co/v1/chat/completions"),
        "keys": ("JEV_COMPARE_BASETEN_KEY", "BASETEN_API_KEY"),
        # Baseten's Model APIs serve the models Baseten hosts; which one
        # stands in for Haiku is the operator's call, so there is no default.
        "model": None,
        "usd": None,
    },
    "claude-cli": {"url": None, "keys": (), "model": "haiku",
                   "usd": (DEFAULT_USD_IN, DEFAULT_USD_OUT)},
}


class Declined(Exception):
    """An arm that ended without an answer, with the ledger's outcome and cause."""

    def __init__(self, outcome: str, cause: str, detail: str = ""):
        super().__init__(detail or cause)
        self.outcome = outcome
        self.cause = cause


# --- shaping ------------------------------------------------------------------
#
# The confidences mirror Kev's `kev/api.py`, which mirrors TypeSafe's reference
# adapter (system-one-adapter 0.2.1): the three arms then report the same
# function of a distribution, and a difference between them is a difference
# in the distribution.

def normalised(values: list) -> list[float]:
    """Clipped to [0, 1] and summing to 1; all zeros reads as uniform."""
    clipped = [min(1.0, max(0.0, float(v))) for v in values]
    total = sum(clipped)
    if total == 0:
        return [1.0 / len(clipped)] * len(clipped)
    return [v / total for v in clipped]


def choice_confidence(p: list[float]) -> float:
    """(p_max - 1/K) / (1 - 1/K): 0 at uniform, 1 at certainty."""
    k = len(p)
    return 1.0 if k == 1 else (max(normalised(p)) - 1 / k) / (1 - 1 / k)


def score_confidence(p: list[float]) -> float:
    """max(0, 1 - E|level - mode| / D), D the mean absolute deviation of a
    uniform distribution over the levels."""
    levels = len(p)
    if levels == 1:
        return 1.0
    p = normalised(p)
    mode = max(range(levels), key=p.__getitem__)
    spread = sum(abs(i - (levels - 1) / 2) for i in range(levels)) / levels
    return max(0.0, 1.0 - sum(pi * abs(i - mode) for i, pi in enumerate(p)) / spread)


def output_cap(question: dict) -> int:
    """The output tokens a full reply to `question` can need.

    The reply repeats every option key with a probability beside it. A token
    is at least one byte, so the reply's UTF-8 length bounds its tokens.
    """
    if question["type"] == "noul":
        widest = {"probability": 0.123456}
    else:
        widest = {"probabilities": {key: 0.123456 for key in options(question)}}
    cap = len(json.dumps(widest, indent=2).encode("utf-8")) + OUTPUT_SLACK
    if cap > MAX_OUTPUT_TOKENS:
        raise Declined("invalid", "invalid",
                       f"a full reply could need {cap} output tokens; "
                       f"Haiku writes at most {MAX_OUTPUT_TOKENS}")
    return cap


def money(value) -> float | None:
    """A reported cost: a finite JSON number of zero or more, else none."""
    if type(value) in (int, float) and math.isfinite(value) and value >= 0:
        return float(value)
    return None


def options(question: dict) -> list[str]:
    """The keys a distribution is over, in the caller's order."""
    if question["type"] == "choice":
        return list(question["criteria"])
    if question["type"] == "score":
        return [str(i) for i in range(len(question["criteria"]))]
    return ["probability"]


def to_answer(question: dict, dist: list[float]) -> dict:
    """A System One answer from a distribution over `options(question)`."""
    if question["type"] == "noul":
        return {"type": "noul", "noul": round(min(1.0, max(0.0, dist[0])), 4)}
    p = normalised(dist)
    keys = options(question)
    if question["type"] == "choice":
        best = max(range(len(p)), key=p.__getitem__)
        return {"type": "choice", "choice": keys[best],
                "confidence": round(choice_confidence(p), 4),
                "probabilities": {k: round(v, 4) for k, v in zip(keys, p)}}
    return {"type": "score", "score": round(sum(i * v for i, v in enumerate(p)), 4),
            "confidence": round(score_confidence(p), 4),
            "probabilities": {k: round(v, 4) for k, v in zip(keys, p)}}


def shaped(answer: dict, question: dict) -> dict:
    """The row's answer, confidence and distribution: numbers only, as the
    Jev row records them."""
    kind = question["type"]
    if kind == "noul":
        value = jev.judged(answer.get("noul"))
    elif kind == "choice":
        value = jev.position_of(answer.get("choice"), question.get("criteria") or {})
    else:
        value = jev.judged(answer.get("score"))
    return {"answer": value, "confidence": jev.confidence_of(answer),
            "probabilities": jev.distribution_of(answer, question)}


# --- the prompt ---------------------------------------------------------------

SYSTEM = (
    "You are a classifier. You read a piece of state and answer exactly one "
    "question about it with probabilities. You never explain and never add "
    "text: you reply with one JSON object that matches the schema you are "
    "given, and nothing else."
)


def render_state(state) -> str:
    return state if isinstance(state, str) else json.dumps(state, indent=2, sort_keys=True)


def prompt(state, question: dict) -> tuple[str, dict]:
    """The user message and the JSON schema for one question."""
    kind = question["type"]
    lines = ["<state>", render_state(state), "</state>", "",
             f"Instructions: {question.get('instructions', '')}"]
    if kind == "noul":
        criteria = question.get("criteria") or {}
        for word in ("true", "false"):
            if isinstance(criteria, dict) and criteria.get(word):
                lines.append(f"Answer {'yes' if word == 'true' else 'no'} means: "
                             f"{criteria[word]}")
        lines += ["", "This is a yes/no question. Reply as "
                  '{"probability": P}, where P is the probability from 0 to 1 '
                  "that the answer is yes."]
        schema = {"type": "object",
                  "properties": {"probability": {"type": "number"}},
                  "required": ["probability"], "additionalProperties": False}
        return "\n".join(lines), schema
    keys = options(question)
    if kind == "choice":
        lines += ["", "Options:"]
        for key, description in question["criteria"].items():
            lines.append(f"- {key}: {description}" if description else f"- {key}")
        lines += ["", "Pick among the options. Reply as "
                  '{"probabilities": {"<option>": P, ...}} with every option '
                  "above as a key and probabilities that sum to 1."]
    else:
        lines += ["", "Levels, lowest first:"]
        for index, level in enumerate(question["criteria"]):
            lines.append(f"- {index}: {render_state(level)}")
        lines += ["", "Place the answer on these levels. Reply as "
                  '{"probabilities": {"0": P, ...}} with every level number '
                  "above as a key and probabilities that sum to 1."]
    inner = {"type": "object",
             "properties": {key: {"type": "number"} for key in keys},
             "required": keys, "additionalProperties": False}
    schema = {"type": "object", "properties": {"probabilities": inner},
              "required": ["probabilities"], "additionalProperties": False}
    return "\n".join(lines), schema


FENCE = re.compile(r"^\s*```(?:json)?\s*(.*?)\s*```\s*$", re.DOTALL)


def finite(value) -> float:
    """A JSON number that is finite. `json.loads` reads NaN and Infinity, and
    `float()` takes a bool or a string; none of those is a probability."""
    if type(value) not in (int, float) or not math.isfinite(value):
        raise ValueError(f"{value!r} is not a finite number")
    return float(value)


def parse_reply(reply, question: dict) -> list[float]:
    """The distribution a reply carries, over `options(question)`.

    A schema is a request and not a guarantee on every transport, so a code
    fence is stripped and the first JSON object in the text is read. Anything
    else is `invalid`: the endpoint answered, and the answer is unusable.
    """
    if isinstance(reply, str):
        text = FENCE.sub(r"\1", reply)
        start, end = text.find("{"), text.rfind("}")
        try:
            reply = json.loads(text[start:end + 1]) if start >= 0 else None
        except json.JSONDecodeError:
            reply = None
    if not isinstance(reply, dict):
        raise Declined("invalid", "invalid", "the reply is not a JSON object")
    try:
        if question["type"] == "noul":
            return [finite(reply["probability"])]
        found = reply["probabilities"]
        keys = options(question)
        # Exactly the options asked about. A transport may ignore the schema,
        # and an option filled in as zero would be scored as an answer.
        if set(found) != set(keys):
            raise KeyError(f"options {sorted(found)} are not {keys}")
        return [finite(found[key]) for key in keys]
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise Declined("invalid", "invalid", f"the reply has no usable probabilities ({exc})")


# --- transports -----------------------------------------------------------------

def http_json(url: str, body: dict, headers: dict, timeout: float) -> dict:
    """POST JSON, return the parsed reply; a failure is a `Declined`."""
    request = urllib.request.Request(
        url, data=json.dumps(body).encode("utf-8"), method="POST",
        headers=dict(headers, **{"Content-Type": "application/json"}))
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        exc.close()
        raise Declined("unavailable", "unavailable", f"HTTP {exc.code}")
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, TimeoutError):
            raise Declined("timeout", "timeout", "timed out")
        raise Declined("unavailable", "unavailable", str(exc.reason))
    except TimeoutError:
        raise Declined("timeout", "timeout", "timed out")
    except OSError as exc:
        raise Declined("unavailable", "unavailable", str(exc))
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        raise Declined("invalid", "invalid", "the response is not JSON")
    if not isinstance(parsed, dict):
        raise Declined("invalid", "invalid", "the response is not a JSON object")
    return parsed


def count(value) -> int | None:
    return value if type(value) is int and value >= 0 else None


def millis(value) -> int | None:
    """A reported latency: Kev sends a float rounded to one decimal, so any
    finite number of zero or more counts, rounded to whole milliseconds."""
    if type(value) in (int, float) and math.isfinite(value) and value >= 0:
        return int(round(value))
    return None


def ask_anthropic(conf: dict, user: str, schema: dict) -> dict:
    reply = http_json(conf["url"], {
        "model": conf["model"],
        "max_tokens": conf["max_tokens"],
        "system": SYSTEM,
        "messages": [{"role": "user", "content": user}],
        "output_config": {"format": {"type": "json_schema", "schema": schema}},
    }, {"x-api-key": conf["key"], "anthropic-version": "2023-06-01"}, conf["timeout"])
    text = "".join(block.get("text", "") for block in reply.get("content") or ()
                   if isinstance(block, dict))
    usage = reply.get("usage") or {}
    return {"reply": text, "tokens_in": count(usage.get("input_tokens")),
            "tokens_out": count(usage.get("output_tokens")), "usd": None}


def ask_openai_compatible(conf: dict, user: str, schema: dict) -> dict:
    """OpenRouter and Baseten: OpenAI-compatible chat completions."""
    body = {
        "model": conf["model"],
        "max_tokens": conf["max_tokens"],
        "messages": [{"role": "system", "content": SYSTEM},
                     {"role": "user", "content": user}],
        "response_format": {"type": "json_schema",
                            "json_schema": {"name": "answer", "strict": True,
                                            "schema": schema}},
    }
    if conf["via"] == "openrouter":
        # Usage accounting: the response then carries the call's cost.
        body["usage"] = {"include": True}
    reply = http_json(conf["url"], body, {"Authorization": f"Bearer {conf['key']}"},
                      conf["timeout"])
    try:
        text = reply["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        raise Declined("invalid", "invalid", "the response carries no message")
    usage = reply.get("usage") or {}
    cost = usage.get("cost")
    return {"reply": text, "tokens_in": count(usage.get("prompt_tokens")),
            "tokens_out": count(usage.get("completion_tokens")),
            "usd": money(cost)}


def ask_claude_cli(conf: dict, user: str, schema: dict) -> dict:
    """`claude -p --model haiku`, in an empty folder with no user settings,
    hooks, plugins, MCP servers, tools or saved session. Its tokens still
    include Claude Code's own system prompt and its latency the start-up of a
    process, which is why the README calls this transport incomparable."""
    argv = [conf["claude"], "-p", "--model", conf["model"],
            "--output-format", "json",
            "--json-schema", json.dumps(schema),
            "--system-prompt", SYSTEM,
            "--tools", "",
            "--strict-mcp-config",
            "--no-session-persistence",
            "--setting-sources", "project"]
    with tempfile.TemporaryDirectory(prefix="jev-compare-claude-") as empty:
        try:
            done = subprocess.run(argv, input=user, capture_output=True, text=True,
                                  cwd=empty, timeout=conf["timeout"])
        except subprocess.TimeoutExpired:
            raise Declined("timeout", "timeout", "claude -p timed out")
        except OSError:
            raise Declined("unavailable", "no-path", "claude is not on PATH")
    if done.returncode != 0:
        raise Declined("unavailable", "unavailable", f"claude -p exited {done.returncode}")
    try:
        result = json.loads(done.stdout)
    except json.JSONDecodeError:
        raise Declined("invalid", "invalid", "claude -p printed no JSON")
    if not isinstance(result, dict) or result.get("is_error"):
        raise Declined("unavailable", "unavailable", "claude -p reported an error")
    usage = result.get("usage") or {}
    counted = [count(usage.get(name)) for name in
               ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")]
    cost = result.get("total_cost_usd")
    reply = result.get("structured_output")
    return {"reply": reply if isinstance(reply, dict) else result.get("result", ""),
            "tokens_in": sum(c for c in counted if c is not None) if any(
                c is not None for c in counted) else None,
            "tokens_out": count(usage.get("output_tokens")),
            "usd": money(cost),
            "server_ms": count(result.get("duration_api_ms"))}


ASK = {"anthropic": ask_anthropic, "openrouter": ask_openai_compatible,
       "baseten": ask_openai_compatible, "claude-cli": ask_claude_cli}


# --- the arms -------------------------------------------------------------------

def timeout_of(env) -> float:
    try:
        value = float((env.get("JEV_COMPARE_TIMEOUT") or "").strip() or DEFAULT_TIMEOUT)
    except ValueError:
        return DEFAULT_TIMEOUT
    return value if math.isfinite(value) and value > 0 else DEFAULT_TIMEOUT


def price(env, name: str, default: float | None) -> float | None:
    """A configured price, else the default; None when neither is set."""
    text = (env.get(name) or "").strip()
    if not text:
        return default
    try:
        value = float(text)
    except ValueError:
        return default
    return value if math.isfinite(value) and value >= 0 else default


# The arms are opt-in, unlike a Jev stage ("unset means on"): an arm sends
# every live Jev request to a second endpoint, and that is a change the
# operator turns on, one arm at a time.

def kev_on(env) -> bool:
    """The Kev arm runs only when `JEV_COMPARE_KEV` is one of `jev.py`'s
    on-words; unset, an off-word or anything else is off."""
    return (env.get("JEV_COMPARE_KEV") or "").strip().lower() in jev.FLAG_ON


def haiku_via(env) -> str:
    """The selected transport, or "" when the arm is off: unset or one of
    `jev.py`'s off-words. Any other word is returned as given; one that names
    no transport is declined as `invalid`, with no call, rather than guessed at."""
    word = (env.get("JEV_COMPARE_HAIKU_VIA") or "").strip().lower()
    return "" if not word or word in jev.FLAG_OFF else word


def wanted(env) -> bool:
    """Whether any arm is on here. `jev.py` asks this before it starts one."""
    return kev_on(env) or bool(haiku_via(env))


def kev_arm(job: dict, env) -> dict:
    event = {"provider": jev.BASELINE_PROVIDER,
             "model": (env.get("JEV_COMPARE_KEV_MODEL") or env.get("KEV_MODEL") or
                       DEFAULT_KEV_MODEL).strip(),
             "usd": 0.0}
    headers = {}
    key = (env.get("KEV_API_KEY") or "").strip()
    if key:
        headers["Authorization"] = f"Bearer {key}"
    payload = dict(job["payload"], model=KEV_REQUEST_MODEL)
    started = time.monotonic()
    try:
        try:
            response = http_json((env.get("JEV_COMPARE_KEV_URL") or DEFAULT_KEV_URL).strip(),
                                 payload, headers, timeout_of(env))
        finally:
            event["duration_ms"] = int(round((time.monotonic() - started) * 1000))
        event.update(jev.usage_of(response))
        event["server_ms"] = millis(response.get("latency_ms"))
        answers = response.get("answers")
        if not isinstance(answers, dict):
            raise Declined("invalid", "invalid", "the response carries no answers")
        event.update(read_answers(job, answers))
    except Declined as exc:
        # The row keeps what was measured before the arm ended: the wait,
        # and the counts when a response arrived.
        exc.event = event
        raise
    return event


def read_answers(job: dict, answers: dict) -> dict:
    """The one answer a single-question call records; nothing for a batch."""
    questions = job["payload"]["questions"]
    if len(questions) != 1:
        return {}
    qid, question = next(iter(questions.items()))
    if not isinstance(answers.get(qid), dict):
        raise Declined("invalid", "invalid", f"no answer for {qid!r}")
    found = shaped(answers[qid], question)
    # An answer object with no usable value (none at all, or a choice key
    # nobody asked about) is as unusable as a missing one; recorded `ok`, it
    # would count as a call that answered and drop out of agreement unseen.
    if found["answer"] is None:
        raise Declined("invalid", "invalid", f"no usable answer for {qid!r}")
    return found


def haiku_conf(via: str, env) -> dict:
    spec = TRANSPORTS.get(via)
    if spec is None:
        raise Declined("invalid", "invalid", f"JEV_COMPARE_HAIKU_VIA={via!r} names no transport")
    model = (env.get("JEV_COMPARE_HAIKU_MODEL") or "").strip() or spec["model"]
    if via == "baseten":
        model = (env.get("JEV_COMPARE_BASETEN_MODEL") or "").strip() or model
    conf = {"via": via, "model": model, "timeout": timeout_of(env),
            "claude": (env.get("JEV_COMPARE_CLAUDE") or "").strip() or "claude"}
    if spec["url"]:
        variable, default = spec["url"]
        conf["url"] = (env.get(variable) or "").strip() or default
    key = ""
    for variable in spec["keys"]:
        key = (env.get(variable) or "").strip()
        if key:
            break
    conf["key"] = key
    return conf


def haiku_arm(job: dict, env, via: str) -> dict:
    event = {"provider": via}
    try:
        return _haiku_arm(job, env, via, event)
    except Declined as exc:
        exc.event = event
        raise


def _haiku_arm(job: dict, env, via: str, event: dict) -> dict:
    started = time.monotonic()
    try:
        conf = haiku_conf(via, env)
        event["model"] = conf["model"]
        if TRANSPORTS[via]["keys"] and (not conf["key"]
                                        or conf["key"].lower() in jev.PLACEHOLDER_KEYS):
            raise Declined("unavailable", "unkeyed", f"no key for {via}")
        if not conf["model"]:
            raise Declined("unavailable", "unkeyed", f"no model named for {via}")
        questions = job["payload"]["questions"]
        if len(questions) > MAX_HAIKU_QUESTIONS:
            raise Declined("invalid", "invalid",
                           f"{len(questions)} questions; the Haiku arm asks at most "
                           f"{MAX_HAIKU_QUESTIONS} per call")
        for qid, question in questions.items():
            if question.get("type") != "noul" and len(options(question)) > MAX_OPTIONS:
                raise Declined("invalid", "invalid",
                               f"{qid!r} has {len(options(question))} options; the "
                               f"ledger records at most {MAX_OPTIONS}")
        state = job["payload"].get("state")
        results: dict[str, object] = {}

        # One request per question, concurrently: Jev answers each question
        # of a request in isolation, and one prompt holding them all would
        # let the model read one answer into another.
        # A reply that arrived and could not be used was still billed, so it
        # keeps its tokens and cost beside the decline.
        def one(qid, question):
            reply = None
            try:
                user, schema = prompt(state, question)
                ask = dict(conf, max_tokens=output_cap(question))
                reply = ASK[via](ask, user, schema)
                reply["answer"] = to_answer(question, parse_reply(reply["reply"], question))
                results[qid] = reply
            except Exception as exc:                 # a defect here is a decline
                if not isinstance(exc, Declined):
                    exc = Declined("invalid", "invalid", repr(exc))
                results[qid] = dict(reply, declined=exc) if reply else exc

        threads = [threading.Thread(target=one, args=item, daemon=True)
                   for item in questions.items()]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
    finally:
        event["duration_ms"] = int(round((time.monotonic() - started) * 1000))
    replies = [results[qid] for qid in questions]
    for name in ("tokens_in", "tokens_out"):
        counted = [r.get(name) for r in replies if isinstance(r, dict)]
        if any(c is not None for c in counted):
            event[name] = sum(c for c in counted if c is not None)
    reported = [r.get("usd") for r in replies if isinstance(r, dict)]
    if reported and all(c is not None for c in reported):
        event["usd"] = sum(reported)
    elif event.get("tokens_in") is not None or event.get("tokens_out") is not None:
        # Haiku's list price is only Haiku's: another model needs its prices.
        spec = TRANSPORTS[via]
        usd_in, usd_out = (spec["usd"] if spec["usd"] and conf["model"] == spec["model"]
                           else (None, None))
        usd_in = price(env, "JEV_COMPARE_HAIKU_USD_IN", usd_in)
        usd_out = price(env, "JEV_COMPARE_HAIKU_USD_OUT", usd_out)
        if usd_in is not None and usd_out is not None:
            event["usd"] = ((event.get("tokens_in") or 0) * usd_in
                            + (event.get("tokens_out") or 0) * usd_out) / 1e6
    servers = [r.get("server_ms") for r in replies if isinstance(r, dict)]
    if servers and all(s is not None for s in servers):
        event["server_ms"] = max(servers)
    failed = next((r if isinstance(r, Declined) else r["declined"] for r in replies
                   if isinstance(r, Declined) or "declined" in r), None)
    if failed is not None:
        raise failed
    event.update(read_answers(job, {qid: r["answer"] for qid, r in zip(questions, replies)}))
    return event


def run_arm(name: str, job: dict, env, work) -> None:
    """Run one arm and write its row, whatever happened."""
    base = {
        "caller": job["caller"], "stage": job["stage"], "arm": name,
        "pair": job["pair"], "question_id": job.get("question_id"),
        "primitive": job["primitive"], "questions": job.get("questions"),
        "changed": "no",
    }
    try:
        event = work()
        event.update(outcome="ok", cause=None)
    except Declined as exc:
        event = dict(getattr(exc, "event", {}) or {})
        event.update(outcome=exc.outcome, cause=exc.cause)
        event.setdefault("provider", name)
        sys.stderr.write(f"jev-compare: {name}: {exc}\n")
    except Exception as exc:                         # never a traceback, always a row
        event = {"provider": name, "outcome": "invalid", "cause": "invalid"}
        sys.stderr.write(f"jev-compare: {name}: {exc!r}\n")
    base.update(event)
    if jev_meter is not None:
        did = jev_meter.record(base, env)
        sys.stderr.write(f"jev-compare: {name}: {did}\n")


def main(argv=None, env=None) -> int:
    env = os.environ if env is None else env
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 1:
        sys.stderr.write("usage: jev_compare.py -|REQUEST_FILE\n")
        return 1
    if argv[0] == "-":
        job = json.load(sys.stdin)
    else:
        path = Path(argv[0])
        try:
            job = json.loads(path.read_text(encoding="utf-8"))
        finally:
            try:
                path.unlink()
            except OSError:
                pass
    work = []
    if kev_on(env):
        work.append(("kev", lambda: kev_arm(job, env)))
    via = haiku_via(env)
    if via:
        work.append(("haiku", lambda: haiku_arm(job, env, via)))
    # Each arm's row needs a ledger that takes it. Without one the call would
    # be paid for and then thrown away, so the arm does not run.
    runnable = []
    for name, fn in work:
        said = jev_meter.ready(name, env) if jev_meter is not None else "no meter"
        if jev_meter is not None and said == jev_meter.READY:
            runnable.append((name, fn))
        else:
            sys.stderr.write(f"jev-compare: {name}: not run, {said}\n")
    threads = [threading.Thread(target=run_arm, args=(name, job, env, fn), daemon=True)
               for name, fn in runnable]
    for thread in threads:
        thread.start()
    # Each arm bounds its own requests. This bounds the child as a whole, so
    # an arm stuck somewhere no timeout reaches cannot keep a process alive.
    deadline = time.monotonic() + timeout_of(env) * 2 + 5
    for thread in threads:
        thread.join(max(0.0, deadline - time.monotonic()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
