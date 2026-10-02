#!/bin/sh
# Send a small fixed set of requests to the two experiment servers.
# Each request carries a user, metadata and a session header, so the traces
# show where caller tags land. Answers go to $AURA_EXPERIMENT_STATE/responses.jsonl.
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
  # The recorder exits 2 when the answer carries an error or no text.
  line=$(python3 -c 'import json,sys
r=sys.argv[1]
try:
    d=json.loads(r); txt=(d.get("choices") or [{}])[0].get("message",{}).get("content"); err=d.get("error")
except Exception:
    txt=None; err=r[:300]
print(json.dumps({"case":sys.argv[2],"port":sys.argv[3],"secs":int(sys.argv[4]),"answer":(txt or "")[:400],"error":err}))
sys.exit(2 if err or not txt else 0)' \
    "$resp" "$2" "$1" "$(( $(date +%s) - t0 ))") || failed=1
  printf '%s\n' "$line" | tee -a "$out"
}

S="$AURA_SINGLE_PORT"
O="$AURA_ORCH_PORT"
send "$S" single-arith  s-arith  "What is 17 multiplied by 23, plus 5?"
send "$S" single-div0   s-div0   "Divide 10 by 0 using your tools and report exactly what the tool returned."
send "$S" single-stats  s-stats  "What is the mean and the median of 3, 7, 8, 10 and 41?"
send "$S" single-direct s-direct "In one sentence, what is a median?"
send "$O" orch-mixed    o-mixed  "Compute the mean of 2, 4 and 9, then multiply it by the sine of 30 degrees."
send "$O" orch-div0     o-div0   "Divide 100 by the result of 5 minus 5, and tell me what happened."
if [ "$failed" -ne 0 ]; then
  echo "scenarios.sh: at least one request failed; see $out" >&2
  exit 1
fi
