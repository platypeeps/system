#!/bin/sh
# llama.cpp helper — the checkout lives in ~/repos/ai/llama.cpp, this folder
# only holds the wrapper and model presets. Server listens on :8084.
# Usage:
#   llama-cpp.sh build                    build with Metal + RPC support
#   llama-cpp.sh download <repo>[:quant] [file]  cache a model, then exit
#   llama-cpp.sh serve [preset]           serve a preset model (list below)
#   llama-cpp.sh cluster [preset]         serve with layers split onto the peer's rpc worker
#   llama-cpp.sh embed [home|work]        embeddings server, optional RPC peer
#   llama-cpp.sh rpc                      run ggml-rpc-server (offload target)
#   llama-cpp.sh peer                     print this profile's rpc peer host:port
#   llama-cpp.sh test                     smoke-test chat completion on :8084
#
# The two machines meet over Tailscale, not the Thunderbolt bridge. macOS 27
# Local Network Privacy denies the unsigned launchd-run llama-server access to
# the directly-connected bridge subnet, and a bare launchd executable cannot
# show a consent prompt. Tailscale traffic (100.x on utun) is not classified
# as local network, so it is exempt. Node addresses come from
# <config>/llama-cpp/.env (lib/config.sh; see .env.example):
# LLAMA_PERSONAL_ADDR and LLAMA_WORK_ADDR are the two nodes' Tailscale IPv4
# addresses, and each profile's peer is the other one.
# The local bind is derived at runtime from `tailscale ip -4`, so it follows
# the node if Tailscale reassigns. Override with LLAMA_RPC_BIND / LLAMA_RPC_PEER.
# The profile comes from the same rule repo-sync uses: REPO_SYNC_PROFILE,
# then the machine-setup state file, then `work` when LLAMA_WORK_MARKER_DIR
# is set and exists, else `personal`.
set -e
# Linked onto PATH by local-bin-links: walk the symlink to the real script so
# the sibling ../lib/config.sh is found.
SELF="$0"
while [ -L "$SELF" ]; do
  link=$(readlink "$SELF")
  case "$link" in
    /*) SELF="$link" ;;
    *)  SELF="$(dirname "$SELF")/$link" ;;
  esac
done
DIR="$(cd "$(dirname "$SELF")" && pwd)"
# shellcheck source=../lib/config.sh
. "$DIR/../lib/config.sh"
st_source_env llama-cpp
LLAMA_DIR=~/repos/ai/llama.cpp
BIN="$LLAMA_DIR/build/bin"
PORT="${PORT:-8084}"
RPC_PORT="${LLAMA_RPC_PORT:-50052}"

# Locate the Tailscale CLI and print this node's IPv4 (100.x). Empty if
# Tailscale is down, which lets require_bind fail with a clear message.
tailscale_ip() {
  for ts in tailscale /opt/homebrew/bin/tailscale /usr/local/bin/tailscale \
            /Applications/Tailscale.app/Contents/MacOS/Tailscale; do
    command -v "$ts" >/dev/null 2>&1 || [ -x "$ts" ] || continue
    "$ts" ip -4 2>/dev/null | head -1 && return 0
  done
  return 1
}

MACHINE_SETUP_PROFILE_FILE="${MACHINE_SETUP_STATE:-$HOME/.config/machine-setup}/profile"
if [ -n "${REPO_SYNC_PROFILE:-}" ]; then
  PROFILE="$REPO_SYNC_PROFILE"
elif [ -r "$MACHINE_SETUP_PROFILE_FILE" ]; then
  PROFILE="$(cat "$MACHINE_SETUP_PROFILE_FILE")"
elif [ -n "${LLAMA_WORK_MARKER_DIR:-}" ] && [ -d "$LLAMA_WORK_MARKER_DIR" ]; then
  PROFILE="work"
else
  PROFILE="personal"
fi
# Print host:port for the named node's rpc worker, or fail naming the variable.
node_addr() {
  eval "addr=\${$1:-}"
  case "$addr" in
    ""|change-me)
      st_missing "$1" llama-cpp .env
      echo "  ($1 is the peer node's Tailscale IPv4)" >&2
      return 1 ;;
  esac
  echo "$addr:$RPC_PORT"
}
# The peer is resolved lazily (require_peer) so commands that never talk to
# it run without the addresses configured.
case "$PROFILE" in
  work) PEER_VAR=LLAMA_PERSONAL_ADDR ;;  # peer is the personal node
  *)    PEER_VAR=LLAMA_WORK_ADDR ;;      # peer is the work node
esac
RPC_PEER="${LLAMA_RPC_PEER:-}"
require_peer() {
  [ -n "$RPC_PEER" ] || RPC_PEER="$(node_addr "$PEER_VAR")" || exit 1
}
RPC_BIND="${LLAMA_RPC_BIND:-$(tailscale_ip || true)}"

# On a machine with the /Volumes/models disk, models live there, not on the
# boot volume. llama.cpp's -hf cache reads HF_HOME (as <HF_HOME>/hub), so export
# it for the server too. A value already in the environment wins, and a machine
# without that disk keeps llama.cpp's default, ~/.cache/huggingface.
if [ -z "${HF_HOME:-}" ] && [ -d /Volumes/models/huggingface ]; then
  HF_HOME=/Volumes/models/huggingface
  export HF_HOME
fi

# Resolve a file inside the Hugging Face model cache by repo and filename.
# The snapshot hash changes on every re-fetch, so glob it instead of pinning
# one. Prints nothing when the file is not cached, which callers treat as
# "feature unavailable" rather than an error.
hf_cached() {
  ls "${HF_HOME:-$HOME/.cache/huggingface}/hub/models--$1/snapshots/"*/"$2" 2>/dev/null | head -1
}

# EAGLE3 draft head for gpt-oss-20b speculative decoding. It ships inside the
# same repo as the 20B but is not the repo's default file, and llama.cpp has
# no --hf-file-draft flag, so -md takes a cache path. The :quant selector does
# not reach it either, because the matcher wants a <repo-name>-<quant>.gguf
# name. Fetch it by explicit filename:
#   llama-cpp.sh download ggml-org/gpt-oss-20b-GGUF eagle3-gpt-oss-20b-Q8_0.gguf
GPT_OSS_DRAFT="$(hf_cached ggml-org--gpt-oss-20b-GGUF eagle3-gpt-oss-20b-Q8_0.gguf)"

# Vision projectors. Each is optional: absent means the preset serves the
# same model text-only rather than failing.
Q38_MMPROJ="$(hf_cached unsloth--Qwen3.8-27B-GGUF mmproj-F16.gguf)"
TIEL_MMPROJ="$(hf_cached peculiar-ragdoll--Tiel-Coder-35B-A3B-GGUF mmproj-BF16.gguf)"
TIEL_MTP_MMPROJ="$(hf_cached peculiar-ragdoll--Tiel-Coder-35B-A3B-GGUF-MTP mmproj-BF16.gguf)"
# The MTP repo ships no projector of its own; fall back to the base repo's,
# which pairs with the same base weights. Untested against the MTP build:
# if it refuses to load, drop the --mmproj and serve text-only.
[ -n "$TIEL_MTP_MMPROJ" ] || TIEL_MTP_MMPROJ="$TIEL_MMPROJ"

preset_args() {
  case "$1" in
    gemma-e2b)        echo "--host 0.0.0.0 -hf ggml-org/gemma-4-E2B-it-GGUF -ngl 99 --load-mode mmap+mlock --flash-attn on" ;;
    embeddinggemma)   echo "-hf ggml-org/embeddinggemma-300M-GGUF -ngl 99 --load-mode mmap+mlock --flash-attn on" ;;
    gpt-oss-20b)
      a="--host 0.0.0.0 -hf ggml-org/gpt-oss-20b-GGUF -c 0 --jinja -ub 2048 -b 2048"
      [ -n "$GPT_OSS_DRAFT" ] && a="$a -md $GPT_OSS_DRAFT -ngld 99"
      echo "$a" ;;
    gemma-26b)        echo "-hf unsloth/gemma-4-26B-A4B-it-GGUF -ngl 99 --load-mode mmap+mlock --flash-attn on" ;;
    qwen3-coder-next) echo "-hf unsloth/Qwen3-Coder-Next-GGUF:BF16 -ngl 99 --load-mode mmap+mlock --flash-attn on" ;;
    qwen3.5-9b)       echo "-hf unsloth/Qwen3.5-9B-GGUF:Q8_0 -ngl 99 --load-mode mmap+mlock --flash-attn on" ;;
    # NOT CACHED: these weights were deleted to reclaim 21 GiB when
    # tiel-coder-small replaced this preset as the default. Invoking it
    # re-downloads them. The tg128/pp128 figures quoted below and in the
    # launch agent were measured on it before the deletion.
    qwen3.6-35b)      echo "-hf unsloth/Qwen3.6-35B-A3B-GGUF -ngl 99 --load-mode mmap+mlock --flash-attn on" ;;
    # Dense 27B with vision. Q6_K_XL rather than a Q4: 128 GB makes the
    # smaller quant pointless here. The mmproj does NOT come down with the
    # weights despite what -hf's help text implies -- fetch it separately:
    #   llama-cpp.sh download unsloth/Qwen3.8-27B-GGUF mmproj-F16.gguf
    # Without it the model still serves, text-only.
    qwen3.8-27b)
      a="-hf unsloth/Qwen3.8-27B-GGUF:UD-Q6_K_XL -ngl 99 --load-mode mmap+mlock --flash-attn on"
      [ -n "$Q38_MMPROJ" ] && a="$a --mmproj $Q38_MMPROJ"
      echo "$a" ;;
    # Agentic-coding MoE, same 35B-A3B shape as qwen3.6-35b, so it inherits
    # that preset's measured speed profile. Also vision-capable; same
    # separate-mmproj caveat as qwen3.8-27b.
    #
    # NOT CACHED: this quant was deleted to reclaim 21 GiB, and
    # tiel-coder-small below is the cached tier of the same weights.
    # Invoking this preset re-downloads them, so expect a long first run.
    tiel-coder)
      a="-hf peculiar-ragdoll/Tiel-Coder-35B-A3B-GGUF:UD-Q4_K_XL -ngl 99 --load-mode mmap+mlock --flash-attn on"
      [ -n "$TIEL_MMPROJ" ] && a="$a --mmproj $TIEL_MMPROJ"
      echo "$a" ;;
    # Same weights as tiel-coder at a smaller 4-bit tier: 16.5 GiB against
    # 20.8, for context headroom. The publisher benchmarks UD-Q4_K_XL, not
    # this tier, so its quality is interpolated rather than measured.
    tiel-coder-small)
      a="-hf peculiar-ragdoll/Tiel-Coder-35B-A3B-GGUF:UD-IQ4_XS -ngl 99 --load-mode mmap+mlock --flash-attn on"
      [ -n "$TIEL_MMPROJ" ] && a="$a --mmproj $TIEL_MMPROJ"
      echo "$a" ;;
    # tiel-coder's weights plus a trained multi-token-prediction head, which
    # llama.cpp uses as a self-draft model. The publisher measures 77.4 ->
    # 94.4 tok/s (1.22x) at 83.3% acceptance on their own box, and an
    # independent run (zephel01, 2026-09-08, RTX 5090, 696 trials) reports
    # ~1.32x. Neither measured Apple Silicon.
    #
    # MTP accelerates DECODE ONLY. Code review is prefill-dominated -- the
    # diff is the input -- so the win is smaller here than the headline, and
    # llama.cpp issue #28790 reports --spec-type draft-mtp causing a large
    # PREFILL regression on at least one build. Benchmark pp, not just tg,
    # before adopting this over tiel-coder:
    #   llama-bench -m <this> -p 128 -n 128 -r 3
    # Tune with --spec-draft-n-max / --spec-draft-p-min and judge by tok/s;
    # a higher acceptance rate measured slower than a lower one.
    tiel-coder-mtp)
      a="-hf peculiar-ragdoll/Tiel-Coder-35B-A3B-GGUF-MTP:UD-Q4_K_XL -ngl 99 --load-mode mmap+mlock --flash-attn on"
      a="$a --spec-type draft-mtp --spec-draft-n-max 1 --spec-draft-p-min 0.0"
      [ -n "$TIEL_MTP_MMPROJ" ] && a="$a --mmproj $TIEL_MTP_MMPROJ"
      echo "$a" ;;
    # Qwen3.8-generation MoE, 177B total / A3B active. NOT CACHED: the
    # weights were measured, then deleted to reclaim 78 GiB. Invoking this
    # preset re-downloads them, so expect a long first run.
    #
    # -c is pinned on purpose. llama-server defaults this model to a
    # 1048576-token context (it trains at 262144); that KV cache will not fit
    # on top of 76 GiB of weights, and the failure is opaque:
    #     llama_decode: failed to decode, ret = -3
    #     srv decode: Compute error. off = 0, n_batch = 2048, ret = -3
    # 65536 and 32768 both load and generate; 65536 is the tested ceiling.
    #
    # Slower than qwen3.6-35b despite both being A3B, because the router
    # reaches into 76 GiB of scattered experts and IQ3_XXS costs more to
    # dequantize. llama-bench, r=3:
    #     flash-next (177B-A3B, IQ3_XXS)  tg128 38.90  pp128  397.64
    #     qwen3.6-35b (35B-A3B, Q4_K_M)   tg128 85.36  pp128 1028.38
    # It also pins 76 GiB resident, so nothing else runs beside it.
    flash-next)       echo "-hf unsloth/Qwen3.8-Flash-Next-GGUF:UD-IQ3_XXS -ngl 99 -c 65536 --flash-attn on" ;;
    # Reranker for the retrieval pair. --pooling rank is what turns the model
    # into a scorer; --rerank exposes /v1/rerank. Serve beside `embed`, not
    # instead of it: embeddings retrieve, this reorders what they return.
    rerank)           echo "-hf ggml-org/Qwen3-Reranker-0.6B-Q8_0-GGUF --rerank --pooling rank -ngl 99 --flash-attn on" ;;
    *)                return 1 ;;
  esac
}
# Read out of `preset_args` rather than written a second time. The case
# labels above are the inventory; a list beside them is a copy that drifts
# the moment a preset is added, and both `--help` and the unknown-preset
# error read this, so the drift is what a user sees.
PRESETS="$(awk '/^preset_args\(\) \{/{f=1;next} f&&/^\}/{exit}
                f&&/^    [A-Za-z0-9._|-]+\)/{sub(/\).*/,""); gsub(/^ +/,"");
                             gsub(/\|/," "); printf "%s ", $0}' "$0" | sed 's/ $//')"
DEFAULT_PRESET=tiel-coder-small

# The rpc worker binds this node's Tailscale address on purpose: the protocol
# has no authentication, and llama.cpp's own README says never to expose it on
# an open network. The tailnet is private, so only the user's own devices can
# reach it. If Tailscale is down there is no 100.x address to bind to, and the
# launch agent retries every ThrottleInterval until it comes back.
require_bind() {
  [ -n "$RPC_BIND" ] && ifconfig 2>/dev/null | grep -q "inet $RPC_BIND " || {
    echo "rpc: $RPC_BIND is not configured on any interface (Tailscale down?)" >&2
    exit 1
  }
}

# `cluster` connects to the peer worker while parsing arguments, before the
# model loads. If the peer is down llama-server aborts (SIGABRT); worse, an
# aborted connect made mid-protocol can crash the worker, so the host's
# KeepAlive respawn and the worker's KeepAlive respawn flap against each
# other and never settle. So do not hand llama-server the --rpc flag until
# the peer is actually reachable, then let it finish initialising: the worker
# accepts TCP before its Metal backend is ready, and connecting into that gap
# is what starts the crash-loop. If the peer never comes up we exit non-zero
# and launchd retries after ThrottleInterval, never launching llama-server.
wait_peer() {
  host=${RPC_PEER%:*}; port=${RPC_PEER##*:}
  i=0
  until nc -z -w 2 "$host" "$port" 2>/dev/null; do
    i=$((i + 1))
    [ "$i" -ge 30 ] && {
      echo "cluster: peer $RPC_PEER unreachable after ~60s; launchd will retry" >&2
      exit 1
    }
    sleep 2
  done
  sleep 15   # let the worker's Metal backend finish coming up (kernel compile)
}

case "$1" in
  build)
    cd "$LLAMA_DIR"
    cmake -B build -DGGML_METAL=ON -DGGML_RPC=ON -DLLAMA_CURL=ON -DCMAKE_BUILD_TYPE=Release
    cmake --build build --config Release -j
    ;;
  download)
    # Fetch into the llama.cpp model cache and exit. Do not use llama-server
    # for this: it binds $PORT, which the cluster agent already holds, and it
    # never returns. llama-tokenize pulls the same -hf repo, prints, and exits.
    [ -n "$2" ] || {
      echo "usage: llama-cpp.sh download <hf-repo>[:quant] [file.gguf]" >&2; exit 1; }
    if [ -n "$3" ]; then
      # -hff pins one file when the repo default is not the wanted one. Some
      # files cannot be loaded standalone -- an eagle3 draft head aborts with
      # "requires ctx_other to be set" -- so the blob lands and the process
      # still exits non-zero. Confirm from the cache, not the exit code.
      "$BIN/llama-tokenize" -hf "$2" -hff "$3" -p "warmup" >/dev/null 2>&1 || true
      [ -n "$(hf_cached "$(echo "$2" | sed 's|/|--|')" "$3")" ] || {
        echo "download: $3 not in cache after fetch" >&2; exit 1; }
      echo "cached: $2 ($3)"
    else
      "$BIN/llama-tokenize" -hf "$2" -p "warmup" >/dev/null
      echo "cached: $2"
    fi
    ;;
  serve|cluster)
    ARGS="$(preset_args "${2:-$DEFAULT_PRESET}")" || {
      echo "unknown preset '$2' — one of: $PRESETS" >&2
      exit 1
    }
    RPC_ARGS=""
    if [ "$1" = cluster ]; then
      require_peer
      RPC_ARGS="--rpc $RPC_PEER"
      echo "cluster: local Metal + rpc worker at $RPC_PEER" >&2
      wait_peer
    fi
    cd "$LLAMA_DIR"
    # shellcheck disable=SC2086
    "$BIN/llama-server" --port "$PORT" $RPC_ARGS $ARGS
    ;;
  embed)
    RPC_ARGS=""
    case "$2" in
      home) RPC_ARGS="--rpc $(node_addr LLAMA_WORK_ADDR)" || exit 1 ;;      # offload to work
      work) RPC_ARGS="--rpc $(node_addr LLAMA_PERSONAL_ADDR)" || exit 1 ;;  # offload to personal
    esac
    cd "$LLAMA_DIR"
    # shellcheck disable=SC2086
    "$BIN/llama-server" --embeddings --port "$PORT" $RPC_ARGS -ngl 99 --load-mode mmap+mlock --flash-attn on
    ;;
  rpc)
    # -d MTL0: only the Metal device. ggml-rpc-server also exposes an
    # Accelerate BLAS device that reports 0 MiB and aborts inside
    # ggml_backend_blas_graph_compute the first time a layer lands on it.
    require_bind
    exec "$BIN/ggml-rpc-server" -H "$RPC_BIND" -p "$RPC_PORT" -c -d MTL0
    ;;
  peer)
    require_peer
    echo "$RPC_PEER"
    ;;
  test)
    curl "http://localhost:$PORT/v1/chat/completions" \
      -H "Content-Type: application/json" \
      -d '{
        "messages": [{"role": "user", "content": "Hello!"}],
        "temperature": 0.7,
        "max_tokens": 100
      }'
    ;;
  -h|--help|help)
    cat <<HELPEOF
usage: llama-cpp.sh build|download <repo>[:quant] [file]|serve [preset]|cluster [preset]|embed [home|work]|rpc|peer|test

  build        cmake build with Metal + RPC support
  download     cache <repo>[:quant] and exit; [file] pins one file in it
  serve        serve a preset model on :8084 (default $DEFAULT_PRESET; presets:
               $PRESETS)
  cluster      like serve, plus --rpc to this profile's peer (\$$PEER_VAR)
  embed        embeddings server, optional RPC offload peer home|work
  rpc          run ggml-rpc-server on $RPC_BIND:$RPC_PORT (be an offload target)
  peer         print the rpc peer this profile ($PROFILE) offloads to
  test         smoke-test chat completion against :8084

Peer addresses come from $SYSTEM_TOOLS_CONFIG/llama-cpp/.env
(copy .env.example there) or the environment:
LLAMA_PERSONAL_ADDR, LLAMA_WORK_ADDR (Tailscale IPv4 of each node),
LLAMA_WORK_MARKER_DIR (optional; its existence selects the work profile).
Overrides: LLAMA_RPC_PEER, LLAMA_RPC_BIND, LLAMA_RPC_PORT, REPO_SYNC_PROFILE.
HELPEOF
    exit 0
    ;;
  *)
    echo "usage: $(basename "$0") build|download <repo>[:quant] [file]|serve [preset]|cluster [preset]|embed [home|work]|rpc|peer|test" >&2
    exit 1
    ;;
esac
