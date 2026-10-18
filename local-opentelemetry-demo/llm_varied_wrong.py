"""Run the demo's llm service with varied wrong answers (sd:5029).

Upstream's llm (chart 0.40.10, app 2.2.0) answers llmInaccurateResponse with
one canned text, for one product only. This wrapper imports that app.py
unchanged and replaces `generate_response`: with the flag on, every product
gets one answer from `WRONG`, picked by its id's SHA-256, so runs repeat.
With the flag off, upstream answers.

collector-values.yaml mounts this file from the `llm-varied-wrong` ConfigMap,
which opentelemetry-demo.sh builds, and runs it as the container command.
An upstream that no longer imports, or lacks a name used here, stops the
container at start with the reason.
"""

import hashlib
import os
import sys

#: Where the image keeps upstream's app.py and its summary files.
UPSTREAM_DIR = "/app"

#: The wrong answers: opposite sentiment, an invented defect, wrong product
#: facts, and an invented recall.
WRONG = (
    "Customers dislike this product: most reviews call it a waste of money "
    "and say they would not buy it again.",
    "Many reviewers report that the housing cracks after a few uses and the "
    "battery overheats when charged overnight.",
    "Reviewers describe it as a waterproof kitchen appliance that weighs "
    "about 40 kilograms and needs a separate power adapter.",
    "Several reviewers say the product was recalled last year and that the "
    "replacement units fail the same way.",
)

#: What the wrapper reads from upstream's module.
NEEDED = ("app", "api", "FlagdProvider", "check_feature_flag", "generate_response",
          "load_product_review_summaries", "product_review_summaries_file_path",
          "inaccurate_product_review_summaries_file_path")


def pick(product_id: str) -> str:
    """The wrong answer for a product: the same one on every run."""
    digest = hashlib.sha256(product_id.encode("utf-8")).digest()
    return WRONG[int.from_bytes(digest[:8], "big") % len(WRONG)]


def load_upstream(folder: str = UPSTREAM_DIR):
    """Upstream's app module, or SystemExit naming what broke."""
    sys.path.insert(0, folder)
    try:
        import app as upstream
    except Exception as exc:
        raise SystemExit(f"llm_varied_wrong: upstream app.py in {folder} does not "
                         f"import: {exc!r}") from exc
    missing = [name for name in NEEDED if not hasattr(upstream, name)]
    if missing:
        raise SystemExit("llm_varied_wrong: upstream app.py lacks "
                         f"{', '.join(missing)}; update this wrapper for the new demo")
    return upstream


def install(upstream) -> None:
    """Replace upstream's `generate_response`; its route looks the name up per call."""
    original = upstream.generate_response

    def generate_response(product_id):
        if upstream.check_feature_flag("llmInaccurateResponse"):
            answer = pick(product_id)
            upstream.app.logger.info(f"varied inaccurate response for {product_id}: {answer}")
            return answer
        return original(product_id)

    upstream.generate_response = generate_response


def main() -> None:
    upstream = load_upstream()
    # Upstream's `__main__` block, which an import skips. Its summary paths
    # are relative to the image's working folder.
    os.chdir(UPSTREAM_DIR)
    upstream.api.set_provider(upstream.FlagdProvider(
        host=os.environ.get("FLAGD_HOST", "flagd"), port=os.environ.get("FLAGD_PORT", 8013)))
    upstream.product_review_summaries = upstream.load_product_review_summaries(
        upstream.product_review_summaries_file_path)
    upstream.inaccurate_product_review_summaries = upstream.load_product_review_summaries(
        upstream.inaccurate_product_review_summaries_file_path)
    install(upstream)
    # Without upstream's debug=True: its reloader would start a second process.
    upstream.app.run(host="0.0.0.0", port=8000)


if __name__ == "__main__":
    main()
