ASSET = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".svg", ".pdf")
RISKY = ("auth", "secret", "token", "credential", "crypt", "password", "permission",
         ".github/workflows/", "install", "migrat", "schema", "makefile", "dockerfile", ".sql")
DOCS = ("docs/",)
TESTS = ("tests/", "test_", "_test.")

def heuristic(state: dict, routed: str) -> str:
    paths = [p.lower() for p in state["changed_paths"]]
    lines = state["lines_moved"]
    if state["path_count"] == 0:
        return "skip"                              # an empty commit
    if lines == 0 and not state["changed_paths_omitted"] \
            and all(p.endswith(ASSET) for p in paths):
        return "skip"                              # binary assets only
    if lines > 800 or any(r in p for p in paths for r in RISKY):
        return "deep"
    if lines <= 20 and all(p.startswith(DOCS) or p.endswith(".md")
                           or any(t in p for t in TESTS) for p in paths):
        return "cheap"
    return routed
