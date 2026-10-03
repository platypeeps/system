"""Summarise the Aura spans in local-genai-traces' raw OTLP JSON file.

Reads every rotated file, keeps resources whose service.name starts with
`aura`, and prints either a summary (resource keys, span kinds, attribute
keys, where tags land, surviving gen_ai keys) or a span tree.
Usage: inspect.py summary|tree RAW_DIR
"""
import collections
import json
import pathlib
import sys


def val(v):
    for k in ("stringValue", "intValue", "doubleValue", "boolValue"):
        if k in v:
            return v[k]
    if "arrayValue" in v:
        return [val(x) for x in v["arrayValue"].get("values", [])]
    return v


def load(raw):
    resources = collections.Counter()
    spans = []
    for path in sorted(pathlib.Path(raw).glob("traces*.jsonl")):
        for line in path.read_text().splitlines():
            if not line.strip():
                continue
            for rs in json.loads(line).get("resourceSpans", []):
                res = {a["key"]: val(a["value"]) for a in rs.get("resource", {}).get("attributes", [])}
                if not str(res.get("service.name", "")).startswith("aura"):
                    continue
                resources[json.dumps(res, sort_keys=True)] += 1
                for ss in rs.get("scopeSpans", []):
                    for s in ss.get("spans", []):
                        spans.append({
                            "service": res.get("service.name"),
                            "trace": s["traceId"],
                            "id": s["spanId"],
                            "parent": s.get("parentSpanId", ""),
                            "name": s["name"],
                            "status": s.get("status", {}).get("code", 0),
                            "status_msg": s.get("status", {}).get("message", ""),
                            "attrs": {a["key"]: val(a["value"]) for a in s.get("attributes", [])},
                        })
    return resources, spans


def summary(resources, spans):
    print("RESOURCES")
    for r, n in resources.items():
        print(f"  {n:3d} batches  {r}")
    print(f"\nSPANS {len(spans)} in {len({s['trace'] for s in spans})} traces")
    by_name = collections.defaultdict(list)
    for s in spans:
        by_name[(s["service"], s["attrs"].get("openinference.span.kind", "-"), s["name"])].append(s)
    for (svc, kind, name), ss in sorted(by_name.items()):
        keys = sorted({k for s in ss for k in s["attrs"]})
        st = collections.Counter(s["status"] for s in ss)
        print(f"\n{svc} | {kind} | {name} | n={len(ss)} status={dict(st)}")
        print("   keys:", ", ".join(keys))
    print("\nTAG PLACEMENT (session.id, user.id, metadata, tag.tags) by span kind")
    tag = collections.Counter()
    for s in spans:
        kind = s["attrs"].get("openinference.span.kind", "-")
        for k in ("session.id", "user.id", "metadata", "tag.tags"):
            tag[(kind, k, k in s["attrs"])] += 1
    for (kind, k, has), n in sorted(tag.items()):
        print(f"  {kind:10s} {k:11s} present={has!s:5s} {n}")
    gen = sorted({k for s in spans for k in s["attrs"] if k.startswith("gen_ai.")})
    print("\nGEN_AI KEYS SURVIVING EXPORT:", gen or "none")
    fin = sorted({k for s in spans for k in s["attrs"] if "finish" in k})
    print("FINISH-REASON KEYS:", fin or "none")
    print("ERROR SPANS:", [(s["service"], s["name"], s["status_msg"][:80]) for s in spans if s["status"] == 2])


def tree(spans):
    kids = collections.defaultdict(list)
    for s in spans:
        kids[s["parent"]].append(s)
    ids = {s["id"] for s in spans}

    def show(s, d):
        a = s["attrs"]
        extra = {k: a[k] for k in ("tool.name", "llm.model_name", "llm.token_count.prompt",
                                   "llm.token_count.completion", "session.id") if k in a}
        out = str(a.get("output.value", ""))[:60].replace("\n", " ")
        print("  " * d + f"- {s['name']} [{a.get('openinference.span.kind', '-')}] st={s['status']} {extra} out={out!r}")
        for c in kids[s["id"]]:
            show(c, d + 1)

    for s in spans:
        if s["parent"] == "" or s["parent"] not in ids:
            print(f"\n=== {s['service']} trace {s['trace'][:8]}")
            show(s, 0)


def main(argv):
    if len(argv) != 3 or argv[1] not in ("summary", "tree"):
        sys.stderr.write("usage: inspect.py summary|tree RAW_DIR\n")
        return 1
    resources, spans = load(argv[2])
    if argv[1] == "summary":
        summary(resources, spans)
    else:
        tree(spans)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
