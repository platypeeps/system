# CI companion input

`writing-sd-plugin.json` is the manifest of the writing pack, the second plugin the
native suites register next to this repository's own. One description string was
genericized for publication, so its bytes differ from the source commit.
`writing-sd-plugin.source.json` records the source commit and path, and the SHA256 of
the file in this directory.
The native suites consume this manifest through `SD_WRITING_MANIFEST`.
CI verifies the digest before running the shared, dashboard, and runner suites.
No cross-repository credential is required.

To update this fixture, copy the manifest from a committed writing checkout, apply the
same scrub, and update the source commit and SHA256 in the adjacent source record.
Then run all three native suites.
