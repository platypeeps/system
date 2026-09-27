# mezmo-webhook

Signs a payload the way a Mezmo webhook source expects (`X-Hub-Signature`,
sha256) and optionally delivers it — the end-to-end test for the source that
`mezmo-pipeline` configures.

## Usage

```sh
# just the header value
MEZMO_WEBHOOK_SECRET=… node mezmo-webhook.js < payload.json

# sign and deliver, printing the HTTP status
MEZMO_WEBHOOK_SECRET=… node mezmo-webhook.js --post "$WEBHOOK_URL" \
  < ../mezmo-test-data-s3/trial_pipeline_testdata_compact.json
```

No `npm install`: there are no dependencies.

The full loop, once `MEZMO_WEBHOOK_SECRET` and the source URL are to hand:

```sh
../mezmo-pipeline/pipeline.sh source webhook     # capture_metadata on, auto_parse off
MEZMO_WEBHOOK_SECRET=… node mezmo-webhook.js --post "$WEBHOOK_URL" \
  < ../mezmo-test-data-s3/trial_pipeline_testdata_compact.json
```

## Gotchas

- **The secret is no longer in this file, and must not go back in.** It used to
  carry a throwaway signing secret and a hardcoded payload inline, which made
  this a demo of HMAC rather than a tool — it could not sign the data this repo
  actually holds. Reading the payload on stdin and the secret from
  `MEZMO_WEBHOOK_SECRET` is what lets it compose with `mezmo-pipeline` and
  `mezmo-test-data-s3`.
- **The `x-hub-signature` dependency is gone.** The signature is an
  HMAC-SHA256 rendered as `sha256=<hex>`, which `node:crypto` does directly.
  Verified identical to `openssl dgst -sha256 -hmac` for the same input, so
  the output is byte-for-byte what the package produced.
- Needs a Node with global `fetch` and top-level `await` (18+); `--post` is
  what uses them.
- With no redirect on stdin it exits 1 with a usage line rather than hanging
  on an empty terminal read.
