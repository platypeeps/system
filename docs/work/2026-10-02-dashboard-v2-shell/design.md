---
title: The v2 shell owns refresh safety, and each page registers itself
created: 2026-10-02
item: sd:2418
---
# Design — The v2 shell owns refresh safety, and each page registers itself

## What each page does today

The survey read every v2 page script on main at e0abce9 (re-surveyed 2026-10-03,
after the last five ports landed). No port pull request is open.

| Page | Generation | Retirement | Selection clears | Failed read | After a write |
| --- | --- | --- | --- | --- | --- |
| Today | none | none | yes, via `shell.reconcile` | clears rows | no writes |
| Tasks | none for the page read | none | yes, via `reconcile` | keeps old rows live | coalesced reread with a write barrier (`wrote`) |
| Management | none | none | no | keeps old rows and objects live | reads again; no barrier |
| Home | none | none | no; one load only | error state only | no reread |
| Fleet Health | yes | yes: `retire(keep)` | yes | clears everything | no writes |
| Briefs | yes | none | yes: `unselect()` | clears everything | no writes |
| Activity | yes | yes, as Health | yes | clears everything | `load()` per write; a failed one clears everything and says nothing landed |
| Reports | yes | yes, as Health | yes | keeps old rows and objects live | shared `reload()`; no write barrier (PR 73 finding r4167523338, open) |
| Contributions | yes | yes, in `adopt()` | yes | load: error only; reread: keeps rows and objects live | `reread(wrote)` per write; says "landed" only for a landed write |
| Documents | none; one load | none | yes | error state only | no writes |
| Research | none; one load | none | yes | error state only | no writes; per-project sources cached |

Health and Activity are the reference. Their pattern has four parts:

- a counter, `const mine = ++generation`, checked after each `await`;
- `retire(keep)`: drop picks and put each gone id back as type `not listed`;
- `reconcile` after drawing;
- on failure, `retire(new Set())` and clear everything.

Copilot's findings on PRs 64 to 73 have four shapes, and the reader removes each:

| Shape | Findings | Removed by |
| --- | --- | --- |
| Older answer draws last | #64 health, #69 activity, #71 briefs, #72 contributions | generation |
| Command or pick on a gone row | #64 health, #69 activity, #72 contributions | retirement |
| Rows or Details kept after an error, an empty answer or a hidden row | #69 (two), #71 briefs, #68 research | failure path and selection rule |
| Write landed, reread failed, page said "not changed" | #72 (two, both fixed) | the reread failure state |
| A bulk write's shared reread started before the last write landed | #73 (open) | the write barrier in `reread()` |

PR 73 adds one race (its refresh) and one stale-object finding (no retirement) to the rows above; both are fixed on main.

## A. The reader

### Where it lives

A new file, `v2/static/read.js`, defines `window.SHELL_READ`. It is a factory
that takes its dependencies as one object: `fetch`, `commands`, `state`,
`reconcile`, `row`. `shell.js` builds the one reader API from it and exposes it
as `shell.read`, with a `build:` comment as for every change from the
design-source shell.

A separate file keeps the change to the design-source shell to a few lines. It
also lets the page tests load the real reader into their stand-in shell, so
each page test exercises the real guards and not a copy.

The shell's command store gains `commands.retire(keep)`. Today `commands.put`
only adds: nothing in the shell can drop an object or a pick. `retire` is the
Health `retire` moved into the shell. The reader calls it, and a page never does.

### The API a page calls

```js
const briefs = shell.read({
  source: '/api/briefs',                      // the document route
  what: 'the briefs',                         // used in the loading and error texts
  adopt: doc => { /* keep doc; return the objects to put and the state */
    BRIEFS = doc.briefs;
    return { objects: BRIEFS.map(b => ({ id: b.id, type: 'brief', label: b.subj })), state: null };
  },
  clear: () => { BRIEFS = []; },              // drop the page's data after a failed read
  draw: () => update(),                       // redraw from the page's data
  list: { rows: () => tbody.querySelectorAll('tr[data-id]'), select: id => show(id) },
  none: 'Select a brief to see its note.',    // Details when nothing is selected
});
briefs.load();      // first read and each refresh
briefs.reread();    // after a landed write; never answers with a read from before it
```

`adopt` returns `{ objects, state }`. `state` is `null`, or a state for
`shell.state` (`partial`, `empty`). A page may also set `error` there for a
reader refusal inside a 200 answer, as Briefs does.

### What `load()` does

1. Number the read: `mine = ++generation`. Set the loading state.
2. Fetch `source` with `Accept: application/json`. A non-2xx answer fails with
   the answer's `error` text or `HTTP <status>`.
3. If `mine !== generation`, return `false`. This applies on success and failure.
4. On success:
   1. Call `adopt(doc)`.
   2. Call `commands.retire(ids)` with the ids of the returned objects, then put each object.
   3. Set the state and call `draw()`.
   4. Settle the selection: on the first read, the `?row=` id; later, the current one.
      If that id is not among the objects, or its row is hidden, `reconcile` picks the first
      visible row. With none, it calls `commands.select(null)` and resets Details to `none`.
5. On failure:
   1. Call `commands.retire(new Set())`, then `clear()` and `draw()`.
   2. Clear the selection and reset Details as above.
   3. Set the error state: "`<what>` were not read: `<reason>`. Reload retries it."

### What `reread()` does

`reread()` runs after a write landed. It must never answer with a document read
before that write landed, so it holds a write barrier, as Tasks' `reread` does:

- A read queued and not yet started is joined.
- A read in flight is joined only when no `reread()` was called after it started.
- Otherwise one read is queued behind the one in flight, and later calls join it.

The queued read is numbered like `load()` when it starts. A bulk run of N
concurrent writes thus ends in at most two reads, and each write's `reread()`
settles after a read that started once that write landed. This closes PR 73's
open finding on Reports. A refused write calls `load()`, never `reread()`:
only `reread()` may say the change landed.

On success it behaves as `load()`. On failure it keeps the rows on screen and
calls `commands.retire(new Set())`, so no command runs on them. It sets a partial
state: "The change landed; `<what>` were not read again: `<reason>`. Reload
reads them." The page's own write toast still reports the write.

### Retirement in the shell

`commands.retire(keep)`:

- removes each picked id not in `keep` from the pick set, then redraws the bulk bar;
- puts each object id not in `keep` back as
  `{ id, type: 'not listed', label: '<label> (no longer listed)' }`;
- clears the selection and resets Details when the selected id is retired.

No command is declared on `not listed`, so the row menu, the Details bar and the
palette offer nothing for it. Health uses this today and has been reviewed.

### What stays with the page

The page draws everything. The reader never touches rows, lamps or counts. It
decides only when a document is current and which objects are live. Pages with
their own sub-reads keep their own guards for them (Tasks' per-item `DET.gen`).

## B. Per-page registration

### One file per page

Each page gets `sd_dashboard/v2/pages/<name>.py`, which defines `PAGE`:

```python
from . import Api, Page

def _read(read):
    from ... import briefs_screen  # imported on first use, as server.py does today
    return briefs_screen.document(now=read.now)

PAGE = Page(
    section="Briefs",                 # must be a name in GROUPS in v2/static/shell.js
    routes=("/briefs",),              # Today also takes "/"
    html="briefs.html",
    api=(Api("/api/briefs", _read),),
    item="sd:2112",
)
```

`Api` takes `path`, or `pattern` for a path with an id; `read` or `write`;
and `query=False`. `read` gets one `Read` object with `connection`, `now`,
`path`, `parameters` and the server's backends (`fleet`, `jobs`, `services`,
`ports`). It returns a document, which is a 200, or `(status, document)`.

The landed pages need each of these shapes:

| Page | API routes it owns | Shape |
| --- | --- | --- |
| Briefs, Home, Documents | `/api/briefs`, `/api/home`, `/api/documents` | `path`, no connection |
| Health, Management, Activity | `/api/health`, `/api/management`, `/api/activity` | `path` and backends (`fleet`, `ports`, `jobs`, `services`) |
| Tasks | `/api/tasks`, `/api/tasks/<n>` | `path` and `pattern`; `(404, …)` for an id past the 64-bit bound |
| Research | `/api/research`, `/api/research/<checkout>` | `path` and a `pattern` whose tail holds `/`; `(404, …)` for no checkout |
| Reports | `/api/reports`, `/api/reports/clean` | `clean` takes `query=True`, checks `before` itself, and maps `WorkflowError` to 400 |
| Contributions | `/api/contributions/page`; write `/api/contributions/task` | the one page-only write on main |
| Today | none | `/api/now` stays in the chain: the classic Today's Now panel reads it too |

Routes that v1 also reads or posts stay where they are: `/api/now`,
`/api/contributions`, `/api/contributions/acknowledge`, `/api/reports/<n>/acknowledge`,
`/api/jobs/…`, `/api/usage` and the rest of `action_route`.

A `write` takes `action_route`'s contract: it gets the payload and the
principal, and returns a callable on a write connection or raises `ValueError`.
`action_route` asks the registry first. `do_POST` keeps its one preflight
(origin, CSRF, body bound, no query) for every POST, so a registered write
needs no check of its own.

`Page` has two optional fields for an old screen:

- `classic={"Reports (classic)": "/operations?area=reports"}` adds a palette entry.
- `takes="/contributions"` serves the old screen at `/classic/contributions`,
  because the new page takes its path. Today (`/`), Contributions and Documents take one on main.
  The v1 navigation in `pages.py` stays as it is. It links Today and Documents
  to the new pages and Contributions to `/classic/contributions`; this item does
  not settle that difference.

### What reads the registry

`sd_dashboard/v2/pages/__init__.py` enumerates its own modules with `pkgutil`
when it loads, as `v2` enumerates `static/` today. A module without `PAGE` fails
the import. `v2/__init__.py` builds `PAGES`, `SECTIONS` and the generated
`sections.js` from the registry:

- **`SECTIONS`.** Built from the registry. A registered section wins over its
  `CLASSIC` entry when `sections.js` is written, so a port need not delete the
  `CLASSIC` line. `CLASSIC` keeps entries only for sections not yet ported, and
  shrinks in a later cleanup.
- **`SCREENS`.** The shared old screens, plus each page's `classic` entries.
- **Docstring.** It no longer lists ports. Each page file carries its own item.

`server.py` gets one dispatch in place of the `if path == "/api/…"` chain:

```python
found = v2.api(path, method)
if found:
    return self._api(found, split, context)  # 403 session, 400 query, CSRF for a write, then the read
```

`_api` holds the session check, the query refusal and, for a write, the CSRF
check now in `do_POST`. The 403 text keeps its form: "Open a dashboard page
before reading `<section>`." Routes not yet moved (`/api/now`, `/api/usage`,
the v1 routes) stay in the chain.

`route()` asks the registry for `takes` paths and serves the old screen at
`/classic/<x>`. It then needs no edit per port.

### Rail order

The rail draws from `GROUPS` alone, as today. `SECTIONS` is a lookup, so its
key order shows nowhere. A page names its section and cannot place it. A test
reads the section names from `GROUPS` in `shell.js`, and fails a registration
whose section is not one of them. A registration cannot rename or reorder a
section; only an edit of `GROUPS`, from the design source, can.

### Tests

- **`tests/test_v2_registry.py`** (new) enumerates the registry. It checks:
  - every route and API path is claimed once;
  - every HTML file exists;
  - every section is in `GROUPS`;
  - every API route answers 403 without a session;
  - every route without `query=True` answers 400 to a query;
  - every write refuses a missing CSRF token;
  - the generated `sections.js` decodes to the registry's map.

  It names no page.
- `test_default_routes.py` keeps its v1 checks. It loses the `SECTIONS` literal,
  the `required` tuple's v2 entries and the v2 part of `OLD_SCREENS`, because
  the registry test covers them.
- `test_v2_today.py` loses the `window.SHELL_PAGES = {…}` literal.
- `test_v2_briefs.py` loses `list(v2.SECTIONS)[:2] == ["Today", "Briefs"]`:
  `SECTIONS` has no order, and the rail order is `GROUPS`.
- Each page's own test asserts its registration:
  `v2.SECTIONS["Briefs"] == "/briefs"`, its API path and its `classic`.

### README

The "Section or screen" table and the palette row go. In their place, one
sentence names `dashboard.sh pages`. That verb prints the map from the
registry: section, address, new or classic. The per-page paragraphs move into
`local-project-dashboard/docs/pages/<name>.md`, one file each for Today, Tasks,
Home, Contributions, Briefs, Health, Research, Documents, Reports and Activity.
Management has no README paragraph today, so it gets no file; writing one is
not this item. A port then adds its own file there, and the README links the
folder. The shell paragraphs (the default design, `/ui/`, `/v2/` redirects,
the classic screens) stay in the README.

## Why not

- **The reader inside `shell.js` only.** It works, but the page tests use a
  stand-in shell, so they would test a copy. A separate file is tested once and
  loaded everywhere.
- **Registration in each `<name>_screen.py`.** `v2` would then import every
  screen module at load. Screens import collectors and each other. A small
  page file that imports its screen on first use keeps load cheap and free of cycles.
- **Order `SECTIONS` by `GROUPS`.** It shows nowhere, so ordering it adds code
  and a second place that knows the order.
- **Adopt the reader on every page here.** Each page has review tests for its
  own guards. Moving nine pages at once makes one review of nine behaviour
  changes. The operator chose one follow-up per page (D5 in `prd.md`).

## Risks

- **The registry PR conflicts with any port in flight.** It deletes the lines
  every port edits. Mitigation: the five ports it waited for have landed, and
  no new port starts until it lands (D1). It moves all eleven pages in one
  mechanical step.
- **A retired object changes a page's command counts.** `commands.list` counts
  objects per type. `not listed` objects appear under their own type and count
  for no command. `commands.targets`, which the design source's checks read, skips them.
- **The reader hides a page's partial state.** `adopt` returns the state, so the
  page still words it. The reader sets only loading, error and reread failure.
