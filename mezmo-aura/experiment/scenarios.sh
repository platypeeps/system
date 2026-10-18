#!/bin/sh
# Send a small fixed set of requests to the two experiment servers, or each
# prompt of $AURA_PROMPTS to both: one per line, blank and # lines skipped.
# Each request carries a user, metadata and a session header, so the traces
# show where caller tags land. Answers go to $AURA_EXPERIMENT_STATE/responses.jsonl,
# each with the Mezmo target ($MEZMO_TARGET) the servers run against.
# aura.sh experiment run sets the variables; run it through that.
# Every request is sent even when one fails; the run then exits 1, so a
# failed request never reads as a complete experiment.
set -e
out="$AURA_EXPERIMENT_STATE/responses.jsonl"
failed=0

send() { # port label session prompt
  body=$(python3 -c 'import json,sys; print(json.dumps({"model":"aura","stream":False,"user":"probe-user","metadata":{"probe_case":sys.argv[1],"tenant":"local-test"},"messages":[{"role":"user","content":sys.argv[2]}]}))' "$2" "$4")
  t0=$(date +%s)
  resp=$(curl -s -m 240 "http://127.0.0.1:$1/v1/chat/completions" \
    -H 'Content-Type: application/json' \
    -H "x-chat-session-id: $3" -H "x-openwebui-chat-id: $3" -d "$body") || {
    echo "scenarios.sh: $2: curl exit $? from 127.0.0.1:$1" >&2
    failed=1
    resp=""
  }
  # The recorder exits 2 when the answer carries an error or no text, and
  # names the case and the error on stderr. Aura answers a provider failure
  # with HTTP 200 and the error as the reply (sd:5073), so that reply is an
  # error too.
  line=$(python3 -c 'import json,re,sys
r=sys.argv[1]
try:
    d=json.loads(r); txt=(d.get("choices") or [{}])[0].get("message",{}).get("content"); err=d.get("error")
except Exception:
    txt=None; err=r[:300]
if not err and txt and re.match(r"The upstream model provider \(.*?\) returned an error", txt):
    err=txt[:400]
print(json.dumps({"case":sys.argv[2],"port":sys.argv[3],"target":sys.argv[5],"secs":int(sys.argv[4]),"answer":(txt or "")[:400],"error":err}))
if err or not txt:
    detail=err if isinstance(err,str) else json.dumps(err) if err else "no answer text"
    print("scenarios.sh: %s: %s" % (sys.argv[2], detail.splitlines()[0] if detail.strip() else "empty reply"), file=sys.stderr)
    sys.exit(2)' \
    "$resp" "$2" "$1" "$(( $(date +%s) - t0 ))" "${MEZMO_TARGET:-}") || failed=1
  printf '%s\n' "$line" | tee -a "$out"
}

S="$AURA_SINGLE_PORT"
O="$AURA_ORCH_PORT"
if [ -n "${AURA_PROMPTS:-}" ]; then
  # A prompt line holds a non-blank character and does not start with #.
  # A file with none would send nothing and read as a complete run.
  grep -Eq '^[^#[:space:]]|^[[:space:]]+[^[:space:]]' "$AURA_PROMPTS" || {
    echo "scenarios.sh: $AURA_PROMPTS holds no prompt" >&2
    exit 1
  }
  n=0
  while IFS= read -r prompt || [ -n "$prompt" ]; do
    case "$prompt" in '#'*) continue ;; esac
    case "$prompt" in *[![:space:]]*) ;; *) continue ;; esac
    n=$((n + 1))
    send "$S" "single-p$n" "s-p$n" "$prompt" < /dev/null
    send "$O" "orch-p$n" "o-p$n" "$prompt" < /dev/null
  done < "$AURA_PROMPTS"
else
  send "$S" single-arith  s-arith  "What is 17 multiplied by 23, plus 5?"
  send "$S" single-div0   s-div0   "Divide 10 by 0 using your tools and report exactly what the tool returned."
  send "$S" single-stats  s-stats  "What is the mean and the median of 3, 7, 8, 10 and 41?"
  send "$S" single-direct s-direct "In one sentence, what is a median?"
  send "$O" orch-mixed    o-mixed  "Compute the mean of 2, 4 and 9, then multiply it by the sine of 30 degrees."
  send "$O" orch-div0     o-div0   "Divide 100 by the result of 5 minus 5, and tell me what happened."
fi
if [ "$failed" -ne 0 ]; then
  echo "scenarios.sh: at least one request failed; see $out" >&2
  exit 1
fi
