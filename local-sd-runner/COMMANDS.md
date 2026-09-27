# Registered commands

The palette reads `~/.local/share/sd/commands.yaml`. No catalog is installed automatically.
Review `commands.example.yaml` before creating a catalog for this machine.
Keep the catalog and every executable owned, regular, and unwritable by other users.
The catalog contains at most 100 entries. Each entry names an exact argument vector.

Use `sd runner commands catalog --screen item --item ITEM` to read commands and current guards.
It is also how to check a hand edit: an entry that breaks a rule is refused by name.
The JSON on stdout lists it under `rejected` with the rule it broke, and stderr prints
one line per refusal, `rejected: NAME: RULE`. The other entries stay registered;
`configured` stays true because there is a palette, it just refused part of it.
A file the palette cannot trust at all (unreadable, wrong version, unsafe permissions)
still reports `configured: false` with the reason. A `prepare` naming a rejected entry
answers with the same rule, so a missing button is never a silent omission.
The dashboard shows the exact `prepare` CLI request as typed fields change.
Copy its complete revision and catalog values. Preparation refuses stale values.
Preparation records an execution note before any process starts.

Read-only worktree commands run through `sd runner commands execute NOTE`.
Mutating worktree commands enter the existing runner queue and use its isolated checkout and owned supervisor.
They keep the item status unchanged. Their completion does not ship or finish the task.
Register only reviewed executables. `mutates: false` is a maintainer declaration, not a filesystem sandbox.
There is no shell fallback or inline program argument.

Native cancel, resume, requeue, and restore entries use the exact vectors in the example.
They target an existing assignment and require its current revision and run UUID.
They do not create a second assignment or scheduler.
Restore destinations must be new absolute paths.
Quarantine clearing, restart, pruning, and discard are not palette operations.

Use `sd runner commands output NOTE` to read bounded output.
Use `--offset NEXT_OFFSET` to read the next chunk from that response.
Use `sd runner commands reconcile NOTE` after a lost response.
Reconciliation observes durable receipts and exact native target state. It never repeats a command.
An uncertain result stays open. A new request is required after cancellation or completion.

Backups include the catalog, execution logs, process receipts, and an evidence checksum manifest.
Restore refuses altered evidence and never overwrites a different live log.
Restored note paths point to the restored state directory.
The original paths and note digests remain in the source snapshot manifest.
Restoration holds block new command preparation and execution until recovery finishes.
