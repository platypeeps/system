#!/usr/bin/env node
// Sign a payload the way a Mezmo webhook source expects (X-Hub-Signature,
// sha256) and optionally deliver it.
//
// This used to be a four-line demo with the payload and a throwaway secret
// baked into the source. That made it an illustration of an algorithm rather
// than a tool: it could not sign the data this repo actually holds, so it
// could not test the webhook source that mezmo-pipeline configures. Reading
// the payload on stdin makes the two compose:
//
//   ../mezmo-pipeline/pipeline.sh source webhook          # configure it
//   MEZMO_WEBHOOK_SECRET=… node mezmo-webhook.js --post "$URL" \
//     < ../mezmo-test-data-s3/trial_pipeline_testdata_compact.json
//
// No dependencies: the signature is an HMAC-SHA256, which node's own crypto
// does. The x-hub-signature package it used to pull in did exactly this and
// produced the same `sha256=<hex>` string.
import { createHmac } from 'node:crypto';
import { readFileSync } from 'node:fs';

const args = process.argv.slice(2);
let postUrl = null;

for (let i = 0; i < args.length; i++) {
  const a = args[i];
  if (a === '-h' || a === '--help' || a === 'help') {
    process.stdout.write(`usage: mezmo-webhook.js [--post URL] < payload.json

Reads the payload on stdin, prints the X-Hub-Signature (sha256) header value.

  --post URL   also POST the payload to URL with the signature attached,
               then print the HTTP status. Without it nothing is sent.

environment:
  MEZMO_WEBHOOK_SECRET   the source's signing key (required)
`);
    process.exit(0);
  } else if (a === '--post') {
    postUrl = args[++i];
    if (!postUrl) {
      console.error('--post needs a URL');
      process.exit(1);
    }
  } else {
    console.error(`unknown argument: ${a}`);
    console.error('usage: mezmo-webhook.js [--post URL] < payload.json');
    process.exit(1);
  }
}

const secret = process.env.MEZMO_WEBHOOK_SECRET;
if (!secret) {
  console.error('MEZMO_WEBHOOK_SECRET is not set — it is the webhook source\'s signing key.');
  console.error('The secret is deliberately not in this file any more: a value committed');
  console.error('here would be a credential in a tracked file.');
  process.exit(1);
}

// Reading fd 0 synchronously keeps this a one-shot filter with no streaming
// bookkeeping. An interactive run with no redirect would otherwise hang
// forever looking like a crash, so that case is caught first.
if (process.stdin.isTTY) {
  console.error('no payload on stdin — pipe one in, e.g.');
  console.error('  node mezmo-webhook.js < ../mezmo-test-data-s3/trial_pipeline_testdata_compact.json');
  process.exit(1);
}
const body = readFileSync(0);
if (body.length === 0) {
  console.error('empty payload on stdin');
  process.exit(1);
}

const signature = `sha256=${createHmac('sha256', secret).update(body).digest('hex')}`;

if (!postUrl) {
  console.log(signature);
} else {
  const res = await fetch(postUrl, {
    method: 'POST',
    headers: {
      'content-type': 'application/json',
      'x-hub-signature': signature,
    },
    body,
  });
  const text = await res.text();
  console.log(`${signature}`);
  console.log(`POST ${postUrl} -> HTTP ${res.status}`);
  if (text) console.log(text);
  if (!res.ok) process.exit(1);
}
