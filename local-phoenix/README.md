# local-phoenix

Arize Phoenix (LLM observability, UI on `:6006`) from a local `.venv`
(gitignored), plus a small instrumented-OpenAI demo client.

## Usage

```sh
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt   # once
./phoenix.sh start    # serve
./phoenix.sh demo     # send one traced chat completion (needs OPENAI_API_KEY)
./phoenix.sh update   # upgrade phoenix + deps, refreeze requirements.txt
```

`requirements.txt` is a full `pip freeze` of the working venv (Python 3.14).
`update` refreezes it automatically; after hand-installing anything else run
`.venv/bin/pip freeze > requirements.txt` yourself and commit.
