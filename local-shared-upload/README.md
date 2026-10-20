# local-shared-upload

Upload links for one shared, public-read S3 bucket that several repositories use.
Someone outside AWS uploads with a plain `curl` through a signed link; anyone reads an uploaded file by its URL.

```sh
./shared-upload.sh link example-repo/reports            # 168 h, 500 MiB per file
./shared-upload.sh link example-repo/reports --expires 24h --max-mb 50
./shared-upload.sh url example-repo/reports/summary.pdf
```

`link` prints the form's `url` and `field` lines, the expiry, a ready `curl` line and the public URL each file gets.
Hand out the `curl` line; the uploader replaces `/path/to/file`.
One link takes many files: S3 names each `<prefix>/<file name>`, and a second upload of the same name replaces the first.
Give each repository or person its own prefix, such as `<repo>/...`, so uses of the bucket do not overwrite each other.

The dashboard's Management > Deploys shows one link under `inbox/` with Regenerate, and the bucket's console page (sd:5405).
It keeps that link in the workflow database; links signed before keep working until they expire.

## Setup

Copy `.env.example` to `<config>/shared-upload/.env` (`<config>` is `$SYSTEM_TOOLS_CONFIG`, default `~/.config/system`).
Set the bucket, its region and the signing profile there; an exported value wins over the file.
The bucket name and profile name stay in that file, never in this repository.

The signing profile belongs to an IAM user whose only right is `s3:PutObject` on the bucket.
Its long-lived keys keep a link valid for its whole lifetime, up to 7 days, the Signature Version 4 limit.
Keys from SSO or a role also sign, but the link stops when they expire; `link` says so on stderr.
`aws configure export-credentials` reads the keys; the link is signed locally and nothing here calls S3.

## What is public

Every object in the bucket is readable by anyone with its URL.
The bucket policy grants `s3:GetObject` to everyone and nothing else, so listing stays closed and a URL must be known.
Put nothing private in the bucket.
A link lets its holder upload any number of files under its prefix until it expires, each up to the size cap.
It cannot read, list or delete, and S3 refuses any form field its policy does not name.

## Revoking

A link cannot be withdrawn by itself.
To stop every outstanding link, deactivate the uploader user's access key in IAM; new links then need a new key.
Delete an unwanted upload with an operator profile, not the uploader.

## Cost

Storage and requests bill to the bucket's account; public downloads bill as data transfer out.
The size cap bounds one upload, not how many a link holder sends.
