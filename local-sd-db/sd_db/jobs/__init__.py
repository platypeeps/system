"""The runnable entry points, one module per job.

They are here rather than in the shell scripts so the shell stays the thing
that schedules and reports, and Python stays the thing that knows the
database. Each module has a `main()` returning an exit status and prints one
line a person reads in a cron mail.
"""
