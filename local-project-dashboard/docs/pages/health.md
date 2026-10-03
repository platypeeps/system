# Health

Health is at `/fleet-health` (sd:2115); `/health` stays the service's own
check. `GET /api/health` (`health_screen.document`) lists the design's nine
areas in its order. Four have a reader: Worktrees (registrations whose directory
is gone, from the fleet child Sessions reads), Attribution (your own commits,
by each repository's `user.email`, of the last five weeks on its default branch
`origin/HEAD`, merges left out, that lack `Authored-with:`; a repository with no
`origin/HEAD` or no `user.email` is named in its own row, not read on its
checkout's HEAD; the walk runs inside a 10-second budget, and past it the area
says it stopped rather than waited on), Ports (Operations > Ports' reader, with
its counts and warnings) and Protection (`protection.rows`, drawn as a matrix
with one column per repository and a table carrying the same cells; an unread
repository shows no cell). Disk, Credentials, Branches, Dependencies and
Security have no collector yet; each shows as unknown and names what it does
not read. Nothing on the page writes: Prune registrations, Attribute, Inspect
listener and Re-run collector are CLI lines for Copy, and Re-check reads the
document again.
