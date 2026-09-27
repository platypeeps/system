"""A stand-in for the `aws` CLI, so the suite can watch what the script does.

It answers the handful of calls `aws-setup.sh` makes, and it records every one
of them -- the arguments, whatever went down stdin, and the credential
environment it was handed. The recording is the point: "the secret never
reaches a command line" is a claim about the calls themselves, and nothing
weaker than the calls can check them.

Three environment variables drive it:

* ``FAKE_AWS_LOG``       one JSON object per call, appended.
* ``FAKE_AWS_STATE``     the profile store, read and written like
                         ``~/.aws/credentials``; also the keys created and
                         deleted so far.
* ``FAKE_AWS_SCENARIO``  what the account answers: ``identities`` maps an
                         access key id to the ARN it authenticates as, a key
                         that is absent authenticates as nobody; ``rules``
                         overrides the default empty success for a named call;
                         ``new_key`` is the pair ``create-access-key`` hands
                         back.

Credential precedence is copied from the real CLI on purpose: an explicitly
passed ``--profile`` removes the environment provider from the chain, so
``AWS_ACCESS_KEY_ID`` in the environment never answers for a named profile.
``AWS_SHARED_CREDENTIALS_FILE`` replaces the store the profile is read from.
"""

import json
import os
import sys
import time

GLOBAL_WITH_VALUE = ("--profile", "--region", "--query", "--output")


def read_json(path, default):
    try:
        with open(path) as handle:
            return json.load(handle)
    except FileNotFoundError:
        return default


def write_json(path, value):
    with open(path, "w") as handle:
        json.dump(value, handle)


def split_args(argv):
    """Pull the global options out, and return them with what is left."""
    options = {}
    rest = []
    index = 0
    while index < len(argv):
        item = argv[index]
        if item in GLOBAL_WITH_VALUE and index + 1 < len(argv):
            options[item.lstrip("-")] = argv[index + 1]
            index += 2
            continue
        rest.append(item)
        index += 1
    return options, rest


def key_id_in_file(path):
    try:
        with open(path) as handle:
            for line in handle:
                name, _, value = line.partition("=")
                if name.strip() == "aws_access_key_id":
                    return value.strip()
    except OSError:
        return None
    return None


def done(stdout="", code=0, stderr=""):
    if stdout:
        sys.stdout.write(stdout if stdout.endswith("\n") else stdout + "\n")
    if stderr:
        sys.stderr.write(stderr + "\n")
    raise SystemExit(code)


def main():
    argv = sys.argv[1:]
    options, rest = split_args(argv)
    state_path = os.environ["FAKE_AWS_STATE"]
    state = read_json(state_path, {"profiles": {}, "created": [], "deleted": []})
    scenario = read_json(os.environ["FAKE_AWS_SCENARIO"], {})
    profiles = state.setdefault("profiles", {})
    profile = options.get("profile", "default")

    piped = None
    if rest[:1] == ["configure"] and len(rest) == 1:
        piped = sys.stdin.read()

    with open(os.environ["FAKE_AWS_LOG"], "a") as log:
        log.write(json.dumps({
            "argv": argv,
            "rest": rest,
            "profile": profile,
            "region": options.get("region"),
            "stdin": piped,
            "env_access_key": os.environ.get("AWS_ACCESS_KEY_ID"),
            "env_profile": os.environ.get("AWS_PROFILE"),
            "env_credentials_file": os.environ.get("AWS_SHARED_CREDENTIALS_FILE"),
        }) + "\n")

    # A call the test wants to catch the script in the middle of. It blocks
    # until it is killed, which is how a test reaches the window between
    # writing a secret to a temporary file and removing it again. It matches
    # the probe and not the identical call `require_admin_account` makes
    # first: only the probe hands its credentials over in a file of its own.
    hang = scenario.get("hang")
    if hang is not None and rest == hang and os.environ.get("AWS_SHARED_CREDENTIALS_FILE"):
        time.sleep(300)

    # A refusal is matched before the built-in answers, so a test can fail a
    # call the fake would otherwise handle itself -- `aws configure` above all.
    # It names the whole call and not a substring of it: refusing every call
    # whose words contain "configure" would refuse the reads as well, and the
    # write is the one under test.
    for refusal in scenario.get("refusals", []):
        if rest == refusal["match"]:
            done(refusal.get("stdout", ""), refusal.get("exit", 1),
                 refusal.get("stderr", "refused by the scenario"))

    if rest[:1] == ["configure"]:
        if rest[1:2] == ["list-profiles"]:
            done("\n".join(sorted(profiles)))
        if rest[1:2] == ["get"]:
            value = profiles.get(profile, {}).get(rest[2])
            if value is None:
                done(code=1, stderr="could not be found")
            done(value)
        if rest[1:2] == ["set"]:
            profiles.setdefault(profile, {})[rest[2]] = rest[3]
            write_json(state_path, state)
            done()
        if len(rest) == 1:
            answers = (piped or "").split("\n")
            names = ("aws_access_key_id", "aws_secret_access_key", "region")
            entry = profiles.setdefault(profile, {})
            # The real CLI writes the credentials file first and the config
            # file second, so a failure between the two persists the key and
            # reports failure anyway. That is what this models.
            partial = scenario.get("configure_fails_after_credentials")
            for name, answer in zip(names, answers):
                if answer != "" and not (partial and name == "region"):
                    entry[name] = answer
            write_json(state_path, state)
            if partial:
                done(code=1, stderr="could not write the config file")
            done()

    if rest[:2] == ["sts", "get-caller-identity"]:
        entry = profiles.get(profile, {})
        # Every call names its profile on the command line, which takes the
        # environment provider out of the chain. The credentials file is the
        # one environment variable left that decides the answer, because it
        # says which store the named profile is read from.
        if os.environ.get("AWS_SHARED_CREDENTIALS_FILE"):
            key_id = key_id_in_file(os.environ["AWS_SHARED_CREDENTIALS_FILE"])
        else:
            key_id = entry.get("aws_access_key_id")
            # A session token in the profile is sent beside the key, and STS
            # refuses the pair once the token has expired. The key itself is
            # still good, which is why a probe from a credentials file of its
            # own passes while the profile does not.
            if entry.get("aws_session_token"):
                done(code=255,
                     stderr="ExpiredToken: the security token included in the request is expired")
        arn = scenario.get("identities", {}).get(key_id)
        if not arn:
            done(code=255, stderr="InvalidClientTokenId: the security token is invalid")
        account = arn.split(":")[4]
        query = options.get("query")
        if query == "Arn":
            done(arn)
        if query == "Account":
            done(account)
        done(json.dumps({"Arn": arn, "Account": account, "UserId": "AIDAEXAMPLE"}))

    if rest[:2] == ["iam", "get-user"]:
        # The ARN IAM really returns, path and all -- the scenario decides
        # whether this user sits at the root path or under one.
        arn = scenario.get("user_arn")
        if not arn:
            done(code=254, stderr="NoSuchEntity: the user does not exist")
        if options.get("query") == "User.Arn":
            done(arn)
        done(json.dumps({"User": {"Arn": arn}}))

    if rest[:2] == ["iam", "create-access-key"]:
        key_id, secret = scenario.get("new_key", ["AKIANEWKEY", "new-secret"])
        state.setdefault("created", []).append(key_id)
        write_json(state_path, state)
        done("%s\t%s" % (key_id, secret))

    if rest[:2] == ["iam", "delete-access-key"]:
        state.setdefault("deleted", []).append(options.get("access-key-id") or rest[-1])
        write_json(state_path, state)
        done()

    joined = " ".join(rest)
    for rule in scenario.get("rules", []):
        if all(token in joined for token in rule["match"]):
            done(rule.get("stdout", ""), rule.get("exit", 0), rule.get("stderr", ""))
    done()


if __name__ == "__main__":
    main()
