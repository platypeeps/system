"""The runtime catalog, trials and review requests."""

from urllib.parse import quote

from sd_db import skills_catalog, workflow

from .controls import form, select
from .listing import Column, Listing
from .markup import join, tag
from .pages import page


def render(connection, *, parameters, now):
    try:
        inventory = skills_catalog.catalog(connection, now=now)
    except (workflow.WorkflowError, OSError, ValueError) as error:
        return page("Skills", "skills", tag("p", str(error), class_="notice"),
                    subtitle="Skills, trials and improvements")
    query, number, selected = Listing.read_query(parameters)
    listing = Listing("skills", [Column("name", "Skill", lambda row: row["name"]),
        Column("description", "Description", lambda row: row["description"]),
        Column("status", "Availability", lambda row: row["status"])], inventory["skills"],
        path="/skills", query=query, page_number=number, selected=selected)
    shown, paged = listing.page()
    cards = []
    for skill in shown:
        name, revision = skill["name"], skill["revision"]
        address = "/api/skills/" + quote(name, safe="")
        availability = "On " + ", ".join(skill["paths"]) if skill["paths"] else "Trial" if skill["status"] == "trial" else "Available to try"
        actions = [form(address + "/review", label="Review skill", command=f"sd skill review {name}", revision=revision, compact=True),
                   tag("a", "Run with agent", href="/backlog?skill=" + quote(name), class_="button-link")]
        if not skill["paths"]:
            if skill["status"] != "trial":
                actions.append(form(address + "/try", label="Start 30-day trial", command=f"sd skill try {name}", revision=revision, compact=True))
            actions.append(tag("details", tag("summary", "Add to a workflow path"),
                form(address + "/promote", select("Path", "path_name", [(key, key.capitalize()) for key in inventory["paths"]], id=f"path-{name}"),
                    label="Queue promotion", command=f"sd skill promote {name} --path PATH", revision=revision)))
        else:
            actions.append(form(address + "/demote", label="Queue removal from paths", command=f"sd skill demote {name}", revision=revision, compact=True))
        installs = [f"{entry['surface']}: {entry['state']}" for entry in skill["installed"]]
        uses = [f"{entry['surface']} · {entry['mode']}: {entry['count']}" for entry in skill["uses"]]
        cards.append(tag("article", tag("h2", name), tag("p", skill["description"]),
            tag("p", availability, class_="status"),
            tag("p", "Trial expires " + skill["trial"]["expires"][:10], class_="hint") if skill["trial"] else "",
            tag("p", "Installed surfaces: " + ("; ".join(installs) or "none recorded"), class_="hint"),
            tag("p", "Recorded use: " + ("; ".join(uses) or "none"), class_="hint"),
            tag("details", tag("summary", "When to use and source"), tag("p", skill["when"]), tag("code", skill["path"])),
            tag("div", join(actions), class_="operation-actions"), class_="operation-card skill-card"))
    return page("Skills", "skills",
        tag("p", "Find the right skill, try it, or improve it through a review.", class_="lead"),
        tag("p", "Trials become available after the next installation. Promotions and accepted improvements run in isolated checkouts and produce a pull request for you to merge.", class_="hint"),
        tag("section", listing.controls(paged), tag("div", join(cards), class_="operations-grid"), listing.pager(paged), class_="listing"),
        subtitle="Skills, trials and improvements")


def review_controls(connection, row, revision):
    if row["kind"] != "skill-review":
        return ""
    state = workflow.item_state(connection, row["id"])
    proposals = [note for note in state["notes"] if note["kind"] == "proposal" and not note["resolved_at"]]
    if not proposals:
        return tag("p", "Review proposals will appear here. An empty completed review has no changes to apply.", class_="hint")
    choices = []
    import json

    for note in proposals:
        try:
            detail = json.loads(note["body"])
        except ValueError:
            continue
        if not isinstance(detail, dict) or not {"path", "line_start", "line_end", "body"} <= set(detail):
            continue
        identifier = f"proposal-{note['id']}"
        choices.append(tag("label", tag("input", type="checkbox", name="notes", value=str(note["id"]), id=identifier),
            f" {detail['path']}:{detail['line_start']}–{detail['line_end']} · {detail['body']}", for_=identifier, class_="selection-option"))
    return tag("section", tag("h2", "Review proposals"),
        tag("p", "Select the changes you want. Apply queues one assignment for the whole selection.", class_="hint"),
        form(f"/api/skill-reviews/{row['id']}/apply", join(choices), label="Accept and apply selected",
             command=f"sd skill apply {row['id']} NOTE_IDS", revision=revision), class_="control-panel")
