"""A `gh` that answers from a recorded state file and journals every write.

`SD_HOLDS_DOUBLE_STATE` names a JSON file shaped like the ones
`tests/test_holds.py` builds; reads answer from it in the shapes the real
endpoints return (recorded from a website repository and `gohugoio/hugo`
on 2026-09-11, trimmed to the fields the watcher reads), writes mutate it in
place and append one line to `<state>.journal`. Applying the writes is what
lets a test run the watcher a second time "against the same double state" and
assert idempotence.

Only the invocations `holds.py` makes are answered; anything else exits 2
naming the argv, so a new call shape fails a test instead of passing by
accident.
"""

import json
import os
import sys
import urllib.parse


def main(argv):
    path = os.environ["SD_HOLDS_DOUBLE_STATE"]
    with open(path, encoding="utf-8") as handle:
        state = json.load(handle)

    def answer(value):
        print(json.dumps(value))
        return 0

    def fail(message, code=1):
        print(message, file=sys.stderr)
        return code

    if argv[:2] == ["repo", "view"] and argv[3:] == ["--json", "viewerPermission"]:
        permission = state.get("viewer", {}).get(argv[2])
        if permission is None:
            return fail(f"GraphQL: Could not resolve to a Repository with the name '{argv[2]}'.")
        return answer({"viewerPermission": permission})

    if argv[:1] != ["api"]:
        return fail(f"gh double: unexpected invocation {argv!r}", 2)
    method = "GET"
    rest = argv[1:]
    if rest[:1] == ["--method"]:
        method, rest = rest[1], rest[2:]
    read_stdin = False
    if rest[1:] == ["--input", "-"]:
        read_stdin = True
        rest = rest[:1]
    if len(rest) != 1:
        return fail(f"gh double: unexpected invocation {argv!r}", 2)
    url = urllib.parse.urlsplit(rest[0])
    route = url.path.strip("/")
    query = dict(urllib.parse.parse_qsl(url.query))
    page = int(query.get("page", "1"))
    parts = route.split("/")

    if route in state.get("errors", {}):
        return fail(state["errors"][route])

    if route == "user":
        return answer({"login": state["viewer_login"]})

    if parts[0] != "repos" or len(parts) < 4:
        return fail(f"gh double: unexpected invocation {argv!r}", 2)
    slug = f"{parts[1]}/{parts[2]}"
    repo = state.get("repos", {}).get(slug)
    tail = parts[3:]

    if tail == ["issues"] and method == "GET":
        if repo is None:
            return fail("HTTP 404: Not Found")
        # `state=all` too: closed pull requests carry records the watcher
        # must still see, and a regression to `state=open` would otherwise
        # be answered with the closed fixtures anyway.
        if query.get("creator") != "dependabot[bot]" or query.get("state") != "all":
            return fail(f"gh double: unexpected invocation {argv!r}", 2)
        return answer(repo["pulls"] if page == 1 else [])

    if len(tail) == 3 and tail[0] == "issues" and tail[2] == "comments":
        if repo is None:
            return fail("HTTP 404: Not Found")
        comments = repo.setdefault("comments", {}).setdefault(tail[1], [])
        if method == "GET":
            return answer(comments if page == 1 else [])
        if method == "POST" and read_stdin:
            body = json.load(sys.stdin)["body"]
            comment = {"id": state["next_comment_id"],
                       "user": {"login": state["viewer_login"], "type": "User"},
                       "author_association": "MEMBER", "body": body}
            state["next_comment_id"] += 1
            comments.append(comment)
            for pull in repo["pulls"]:
                if pull["number"] == int(tail[1]):
                    pull["comments"] = pull.get("comments", 0) + 1
            journal(path, {"method": method, "path": route, "body": body, "id": comment["id"]})
            save(path, state)
            return answer(comment)

    if len(tail) == 3 and tail[:2] == ["issues", "comments"] and method == "PATCH" and read_stdin:
        body = json.load(sys.stdin)["body"]
        wanted = int(tail[2])
        for comments in (repo or {}).get("comments", {}).values():
            for comment in comments:
                if comment["id"] == wanted:
                    comment["body"] = body
                    journal(path, {"method": method, "path": route, "body": body, "id": wanted})
                    save(path, state)
                    return answer(comment)
        return fail("HTTP 404: Not Found")

    if len(tail) == 3 and tail[0] == "collaborators" and tail[2] == "permission":
        grant = repo.get("permission", {}).get(tail[1]) if repo else None
        if grant is None:
            return fail("HTTP 404: Not Found")
        return answer({"permission": grant["permission"], "role_name": grant["role_name"],
                       "user": {"login": tail[1]}})

    if len(tail) == 2 and tail[0] == "issues" and method == "GET":
        issue = state.get("issues", {}).get(f"{slug}#{tail[1]}")
        if issue is None:
            return fail("HTTP 404: Not Found (HTTP 404)")
        return answer({"number": int(tail[1]), "state": issue["state"], "title": issue.get("title", ""),
                       "html_url": issue.get("html_url", f"https://github.com/{slug}/issues/{tail[1]}")})

    return fail(f"gh double: unexpected invocation {argv!r}", 2)


def journal(path, entry):
    with open(path + ".journal", "a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry) + "\n")


def save(path, state):
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(state, handle, indent=2)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
