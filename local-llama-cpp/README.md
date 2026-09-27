# local-llama-cpp

Wrapper around the llama.cpp checkout in `~/repos/ai/llama.cpp` (built with
Metal + RPC). Server always listens on `:8084`. Linked onto `PATH` as
`llama-cpp` by `local-bin-links`.

On a machine with the `/Volumes/models` disk, the wrapper exports
`HF_HOME=/Volumes/models/huggingface` unless the environment already sets it,
so `-hf` downloads and cached-file lookups both use `/Volumes/models/huggingface/hub`.
Without that folder, llama.cpp's default `~/.cache/huggingface` stays in use.

## Usage

```sh
llama-cpp build                  # cmake build (Metal + RPC + curl)
llama-cpp serve [preset]         # default preset: tiel-coder-small
llama-cpp cluster [preset]       # serve, with layers split onto the peer's rpc worker
llama-cpp embed [home|work]      # embeddings server, optional RPC offload peer
llama-cpp download <hf-repo>     # fetch model via -hf
llama-cpp rpc                    # ggml-rpc-server, offload target for the other machine
llama-cpp peer                   # print the rpc peer this profile offloads to
llama-cpp test                   # curl smoke test against :8084
```

Presets: run `llama-cpp --help`, which reads them out of `preset_args` at
runtime. A list written here was already three presets behind, which is why
there is no longer one.

## Serving agent (on demand)

An optional launch agent can run `serve tiel-coder-small` on :8084 on demand,
labelled `$SYSTEM_TOOLS_LABEL_PREFIX.llama-cluster` (prefix default
`local.system-tools`). It does not start at login and does not restart itself.
Start it with `launchctl kickstart gui/$(id -u)/<label>`.
Stop it with `launchctl kill TERM gui/$(id -u)/<label>`.
The model holds 16.5 GB with mlock while it runs.

## Two-machine cluster

Both profiles (`personal`, `work`) can run `llama-cpp rpc` as a launch agent
(`<prefix>.llama-rpc`). Each worker binds its own Tailscale address, and
`cluster` on either machine points `--rpc` at the other. The two addresses
live in `.env` (copy `.env.example`):

| profile | worker binds | `cluster` offloads to |
| --- | --- | --- |
| personal | `tailscale ip -4`, port 50052 | `$LLAMA_WORK_ADDR:50052` |
| work | `tailscale ip -4`, port 50052 | `$LLAMA_PERSONAL_ADDR:50052` |

`cluster`, `peer` and `embed home|work` fail naming the missing variable when
the peer address is not set; the other commands do not need it.

llama.cpp splits weights and KV cache across local Metal and the remote
worker in proportion to free memory; `--tensor-split` overrides that. Override
the addresses with `LLAMA_RPC_BIND`, `LLAMA_RPC_PEER`, `LLAMA_RPC_PORT`. The
profile is `REPO_SYNC_PROFILE`, then the machine-setup state file, then `work`
when `LLAMA_WORK_MARKER_DIR` exists, else `personal`.

## Gotchas

- The rpc worker exposes only the Metal device (`-d MTL0`). `ggml-rpc-server`
  also offers an Accelerate BLAS device that reports 0 MiB and aborts inside
  `ggml_backend_blas_graph_compute` the first time a layer lands on it.
- `rpc` refuses to start when the bind address is not configured on any
  interface (Tailscale down). The agent's `KeepAlive` retries every 30 s, so
  it comes up on its own once Tailscale is back.
- The RPC protocol has no authentication. Never bind it to a LAN or
  `0.0.0.0`; the tailnet reaches only your own devices.
- On Thunderbolt 5 with macOS 26.2+ the transport negotiates RDMA on its own
  (`rdma_ctl enable` once from Recovery); otherwise it falls back to TCP.
- `gemma-e2b` and `gpt-oss-20b` bind `0.0.0.0` (LAN-reachable); the rest bind localhost.
