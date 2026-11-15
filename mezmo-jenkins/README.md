# mezmo-jenkins

Read a Mezmo pull request's Jenkins build from the shell (sd:8796).

```sh
./jenkins.sh log owner/repo#12        # console of the newest build
./jenkins.sh log owner/repo#12 3      # console of build 3
./jenkins.sh wait owner/repo#12 --timeout 30
./jenkins.sh test                     # unit tests
```

`wait` polls the newest build until it finishes, prints `build N: RESULT`,
and on a result other than `SUCCESS` prints its failing lines (`FAIL`,
`error`). It exits 0 on `SUCCESS` and 1 on any other result or a timeout
(default 60 minutes). A PR with no build yet is waited for.

## Config

Copy `.env.example` to `<config>/mezmo-jenkins/.env` (`<config>` is
`$SYSTEM_TOOLS_CONFIG`, default `~/.config/system`), or export the values:

| Variable | Meaning |
| --- | --- |
| `JENKINS_URL` | the Jenkins base URL, its `*.sdm.network` name |
| `JENKINS_JOB_PATH` | a PR job's path, with `{owner}`, `{repo}`, `{pr}` |

The `*.sdm.network` name can fail to resolve while the StrongDM client is
connected. Then the script takes the local port that `sdm status` lists
for that name (`JENKINS_SDM` names the binary; default the SDM app's).
Every request is a `curl --max-time` (`JENKINS_CURL_SECS`, 30).
