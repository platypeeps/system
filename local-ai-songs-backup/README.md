# local-ai-songs-backup

Selective nightly backup of WAV, MP3 and MP4 files beneath a source folder
(`AI_SONGS_SOURCE`) into a backup folder (`AI_SONGS_DESTINATION`).

Set both in `$SYSTEM_TOOLS_CONFIG/ai-songs-backup/.env` (copy `.env.example`)
or export them. A missing value fails with its name and both remedies.

Relative paths are preserved. The destination is an exact selective mirror:
files absent from the source, and files with other extensions, are deleted.
The script refuses to run when no matching source files exist.

```sh
./ai-songs-backup.sh list
./ai-songs-backup.sh plan
./ai-songs-backup.sh sync
```

A nightly `local-cron-jobs` entry can run `sync`; `local-mirror-sync` can
then copy the destination folder to an off-machine backup.
