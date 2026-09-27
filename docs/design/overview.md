---
title: How this all fits together
eyebrow: Start here
stand: Two repositories, one database, and five workflows that share them. This page is the map; each workflow has its own page.
---

## The two repositories

**`platypeeps/system-tools`** holds the tools: one folder per tool, each
with its own entrypoint and `README.md`. Local services, scheduled jobs, the
dashboard, the runner.

**`platypeeps/sd-ai-command-pack`** is public and holds the commands that work
gets done with: `sd`, `sd-review`, `sd-check`, `sd-ship`, `sd-status`,
`sd-docs-lint`, `sd-research-kit`, and the skills and standards they enforce.

Neither is copied into the other. `local-bin-links` symlinks both onto `PATH`
from wherever they are checked out, reading the pack's `bin/` on every run
rather than keeping a list — because two hand-written rows once stood for a
checkout shipping seventeen commands.

The dependency runs **both** ways, which is the part that surprises people. The
pack tells its readers to run `adversarial-gate`, and that command lives
in this repository. When a document moves, check what it tells the
reader to *run*, not only what it says.

@diagram system-overview

## The one database

`sd_db` is a single SQLite database, installed as a package and read by
everything: the dashboard, the runner, the scheduled jobs, and every `sd`
command. No network call is needed to answer "what is the state of this".

That is why tracking moved into it. This repository has no GitHub issues and is
to keep it that way — work is captured with `sd task add --here`, and
`docs/work/.status-source` says `row`. Pull requests are still how code lands
and how review happens; it is *tracking* that moved.

The catch worth knowing up front: **writing a `docs/work` folder does not
create its row.** Nothing does it for you. See
[The coding workflow](coding-workflow.html).

## The five workflows

| Page | What it covers |
|---|---|
| [The coding workflow](coding-workflow.html) | A change from folder, to row, to branch, to gates, to a merged commit — and the two separate merge permissions |
| [The writing workflow](writing-workflow.html) | Six stages, three gates, and why a readiness decision expires when the draft changes |
| [The research workflow](research-workflow.html) | One standard across the research repos, three mechanical checks, and the adversarial pass |
| [The code review setup](code-review.html) | How a diff is routed, why the deterministic check runs first, and what "blocking" actually means |
| [The scheduled jobs](jobs.html) | What a job is made of, which machine runs which, and the three records every run leaves |

## Four ideas they have in common

These recur in every workflow, and they explain most of the design decisions.

**1. Enumerate from the machine, not from prose.** A list written in a document
is wrong from the first time something is added. The CI preflight globs
`*/tests/test_*.py` rather than trusting the workflow's own list of suites. The
health-check sweep probes every folder's `help` output rather than reading a
list of tools. `sd-research-kit pins` reads the asserted shas out of the
documents on every run. The job table on the jobs page is generated at build
time from the job files.

This page follows the rule: it names two repositories and five pages, which are
the things it actually links. It does not count the tools, the jobs, the test
suites or the services.

**2. A gate that cannot run has not passed.** A check that silently does
nothing and prints a pass is worse than no check. A research repo with no
`CLAUDE.md`, or a pack install missing its `skills/`, fails review rather than
passing quietly. A drift-reporting stage that prints only a human-readable
remedy makes the drift counter lie.

**3. State the evidence, and say what you could not check.** Every document
here ends with a Status section separating what was verified from what was not.
Research documents are required to. A claim with no source is cut or moved into
Status — never left in the body, where a reader assumes it was checked.

**4. Nothing depends on the optional parts.** `local-jev` puts a judgment model
on `PATH`, and every caller keeps the mechanism it had. The machine runs the
same without it as with it. The kill switch is a file rather than a variable,
because cron and launchd read no shell profile — and an absent file means
*enabled*, because a switch that defaults to off makes every integration added
after it silently never run.

## Reading the pages

Each page opens with what it is about, carries one diagram, and ends with a
Status section. The diagrams are rendered ahead of time and embedded; each has
an interactive version — theme switching, view navigation and image export —
linked beneath it.

These pages describe the design. They are not the authority on any live value:
where a number or a setting is machine-specific, the page names the command
that answers it rather than printing an answer that will rot.

## Status

**Verified in this build.** The two-way dependency between the repositories and
the `local-bin-links` mechanism; that `docs/work/.status-source` reads `row`;
the five linked pages exist in this document set. Every specific claim in the
four ideas above is verified on the page that makes it.

**Not verified here.** Nothing on this page is a live machine reading. For
current state, follow the commands named on each workflow page.
