# mezmo-test-data-s3

Test data for Mezmo trial pipelines (S3 ingest scenario): 100 sample log
records as compact JSON, plus the prompt files used to generate and extend the
set. Data only — no entrypoint.

## Using it

Pretty-print it when you need to read it, rather than storing a second copy:

```sh
jq . trial_pipeline_testdata_compact.json | less
```

Feed it at a configured webhook source, which is the end-to-end test for
`mezmo-pipeline`'s `source webhook`:

```sh
MEZMO_WEBHOOK_SECRET=… node ../mezmo-webhook/mezmo-webhook.js --post "$WEBHOOK_URL" \
  < trial_pipeline_testdata_compact.json
```

Put it in a bucket for the S3 ingest scenario the folder is named for:

```sh
aws s3 cp trial_pipeline_testdata_compact.json "s3://$BUCKET/$PREFIX/"
```

## Gotchas

- **The pretty-printed copy is gone.** `trial_pipeline_testdata_pretty_print.json`
  held byte-identical content to the compact file — the same 100 records, 276 KB
  of second copy with nothing to keep the two in step, and no script that
  regenerated either. `jq .` is the generator. Recover it from git history if
  some tool genuinely needs a file rather than a pipe.
- `Mezmo Developer Docs - 2.8 (1).pdf` (61 MB) is gitignored — over GitHub's
  50 MB warning limit. It stays local only.
- `trial_pipeline_testdata-prompts.txt` is a stub (one empty bullet); the real
  prompt is `trial_pipeline_testdata-error.prompt`.
