# stalled

Count the days since the last success of a scheduled notebook or a webhook, and say so when the count is too high.

Here is how a scheduled job dies without a sound. A scheduled notebook refreshes a layer every
night. One night it starts to fail, because a password changed or an input moved. The Esri
documentation says a task that fails five times is stopped. Nobody gets a page when that
happens. The task list shows a status, and no one opens the task list. A webhook goes the same
way. Its receiver is down for a while, the deliveries fail, and the server deactivates the
webhook. The layer behind the map goes stale. Weeks later a user asks why the map is old. The
first question is "when did it last work?".

Esri's pages answer part of that question. The Manage notebook tasks window shows no last-run
column. The documented `tasks` REST resource gives a task state and the time of the last start,
and a start is not a success. The ArcGIS API for Python lists up to 30 runs per task with a
status. The webhook resource shows `active`, and its failure log shows failures, never a success.
See "Esri behaviour this relies on".

stalled does not poll any of those. It keeps its own record, on purpose: one format for notebooks
and webhooks, and no admin token needed to read it. The job writes one line to a ledger when it
ends. A daily check reads the ledger, counts the days since the last success, and exits non-zero
when the count is over the limit. It also exits non-zero when a notebook has five failures after
its last success, because Esri has stopped that task. For a service webhook, the check also reads
the server and reports a webhook that is switched off, even when its last success was yesterday.

```console
$ python stalled.py --ledger runs.jsonl --webhooks-url http://127.0.0.1:18765/arcgis/admin/services/S.FeatureServer/webhooks --with-failures --expect nightly-refresh=1.5 --expect weekly-export=8 --expect tile-rebuild=3 --now 2026-10-13T12:00:00Z
stalled report as of 2026-10-13T12:00:00Z
JOB                            VERDICT      DAYS  LAST SUCCESS         NOTE
nightly-refresh                OK            1.4  2026-10-12T03:00:00Z
tile-rebuild                   STALLED       8.3  2026-10-05T04:00:00Z
webhook:a1b2                   OK            0.1  2026-10-13T09:00:00Z (parcel-sync) 1 logged failure(s), latest 2025-10-09T08:53:20Z
webhook:c3d4                   OFF           7.1  2026-10-06T09:00:00Z (permit-feed) 1 logged failure(s), latest 2025-10-09T08:53:20Z
weekly-export                  STOPPED      11.2  2026-10-02T06:00:00Z 5 failures since the last success. A scheduled notebook task is stopped after 5.
5 job(s): 2 OK, 1 STALLED, 1 STOPPED, 1 OFF
```

That block is a real run of `--webhooks-url ... --with-failures` against a local stub server. The
stub returns two webhooks, one with `active` set to false. It is not output from a live ArcGIS
Enterprise site. See "Limits".

## The alternative, and what it does well

[healthchecks](https://github.com/healthchecks/healthchecks) (BSD-3-Clause, also a hosted
service) is an open source cron and background task monitor. It is the right tool when a job can
send an HTTP request or an email. A job pings it on success. If no ping arrives within the period
plus a grace time, it sends alerts. It has a web dashboard, an API and 25 or more notification
integrations. stalled has none of those, and it sends nothing anywhere.

Use healthchecks if you can. Use stalled when you cannot: the host can send no ping, you cannot
add a service, or you want the ArcGIS-specific parts. Those parts are the webhook `active`
state and the five-failure stop of a scheduled notebook task. Esri's own pages are the other
alternative. The Manage notebook tasks window shows the status of each task and a View details
list of recent runs. The webhook notification status lists failed deliveries. Both need a person
to look.

Name check: `gh search repos stalled` on 2026-10-13 found no ArcGIS tool with this name. Searches
for ArcGIS notebook and webhook monitors found
[Esri/webhooks-samples](https://github.com/Esri/webhooks-samples) (sample receivers) and a few
webhook listeners, and no tool that counts days since a last success. That search is not proof
that none exists.

## Self-test

```console
$ python stalled.py --self-test
stalled self-test: no ArcGIS, no credentials, loopback stub only
--------------------------------------------------------------------
PASS  a UTC time with Z parses
PASS  a time formats back to the same text
PASS  a time with no zone is REFUSED  <-- pinned defect
...
PASS  five failures after a success inside the limit is STOPPED, not OK  <-- pinned defect
...
PASS  6 writers appending at once lose no line  <-- pinned defect
...
PASS  a unique prefix --ap of --apply is REFUSED by the parser  <-- pinned defect
...
PASS  a server that echoes the token back gets it redacted  <-- pinned defect
...
PASS  an http proxy in the environment is bypassed for the loopback stub  <-- pinned defect
...
--------------------------------------------------------------------
186 assertions, 0 failed
```

The run gives 186 assertions on Windows Python 3.13, Windows Python 3.9, Linux Python 3.12 and
Linux Python 3.9. The outputs are identical line for line, apart from the Windows line endings.
Branch coverage is 100 percent on Windows 3.13 and on Linux 3.12 (see "Limits" for the one
pragma).

## Requirements

Python 3.9 or later, standard library only. No install, no config file, no ArcGIS software.

```
git clone https://github.com/uhsear/stalled.git
```

## Usage

Step 1. Make the job record its own outcome. Put this as the last step of the job, after the real
work has ended. `--apply` is required, because it is the only flag that writes.

```
python stalled.py --ledger runs.jsonl --record nightly-refresh --apply
python stalled.py --ledger runs.jsonl --record nightly-refresh --outcome fail --apply
```

A job that can fail should record both outcomes. This shell wrapper does it, and a failure then
leaves a line:

```
if python job.py; then OUT=ok; else OUT=fail; fi
python stalled.py --ledger runs.jsonl --record nightly-refresh --outcome $OUT --apply
```

For a webhook, run the same command from the receiver, with the job name `webhook:<id>`. The id is
the `id` that the server lists for that webhook. A webhook records a success only when it
delivers. If the layer behind it can sit quiet for days, raise its limit with `--expect`.

Step 2. Run the check each morning, from the scheduler of your choice.

```
python stalled.py --ledger runs.jsonl --expect nightly-refresh=1.5 --expect weekly-export=8
python stalled.py --ledger runs.jsonl --webhooks-url https://HOST/arcgis/admin/services/NAME.FeatureServer/webhooks --with-failures
```

| Flag | Default | What it does |
|---|---|---|
| `--ledger` | none | The run ledger. One JSON line per run. Required. |
| `--expect NAME=DAYS` | none | The most days a job may go without a success. Repeat for more jobs. A job named here with no record is `NEVER`. |
| `--default-days` | `2` | The limit for a job with no `--expect`. |
| `--now` | the clock | Judge as of this UTC time. Use it for a repeatable run. |
| `--webhooks-url` | none | A service webhooks URL that ends in `/webhooks`. Read only. |
| `--with-failures` | off | Also read each webhook's `notificationStatus` (ArcGIS Enterprise 11.2 or later). |
| `--record NAME` | none | Append a run of this job to the ledger. |
| `--outcome` | `ok` | The outcome `--record` writes: `ok` or `fail`. |
| `--when` | the clock | The UTC time `--record` writes. A time more than 5 minutes ahead is refused. |
| `--apply` | off | Write the `--record` line. Without it nothing is written. |
| `--self-test` | off | Run the offline assertions and exit. |

Verdicts: `OK`, `STALLED` (older than the limit), `STOPPED` (a notebook job with five failures
after its last success, so Esri has stopped its task), `NEVER` (no success on record), `OFF` (the
server lists the webhook as not active) and `FUTURE` (a success dated more than 5 minutes after
now). Only `OK` is good. A job that Esri stopped and you then switched on again stays `STOPPED`
until it records a success.

Exit codes: 0 every job is `OK`, or a `--record` run finished (a run without `--apply` prints
"check only" and also exits 0). 1 at least one job is not `OK`. 2 the tool could not decide: the
ledger is missing or torn, the read failed, nothing was there to judge, or the write failed. 64
usage error, including an unknown flag.

The ledger line is `{"job": "nightly-refresh", "ok": true, "ts": "2026-10-13T03:00:00Z"}`. The time
is UTC with a `Z`. A time with no zone is refused, because it could be local time. Write the
ledger only through `--record --apply`: the file must be plain ASCII.

An append creates the file `<ledger>.lock` for a moment, so that two runs cannot overwrite each
other, and deletes it again. A run waits up to 10 seconds for the lock, then exits 2. A lock file
older than 60 seconds is taken as a crashed run's and is removed. The repo ignores `*.lock`.

The webhook token comes from the `STALLED_TOKEN` environment variable only. There is no flag for it,
so it cannot land in a shell history or a process list. The tool sends it in the
`X-Esri-Authorization` header and never in a URL. It refuses a URL that holds a query string, a
login or a fragment. It refuses plain `http` to any host except the local machine. It does not
follow a redirect, because the header would go to the new host. It never sends a request to an
`http` proxy, so the token cannot reach one in clear text. If a server repeats the token in its
error text, the tool shows `***` in its place.

## Why the obvious version is wrong

The first version people write checks the last line of a log. That fails three ways. A job that
dies before it writes anything leaves an old success line, which reads as healthy for as long as
the log is kept. A ledger with a torn last line is easy to skip, and the skipped line is often the
failure. A job that has never run has no line at all, and "no data" prints as nothing.

stalled treats each of these as a finding. A job with no success is `NEVER`, and it exits 1. A
torn ledger line stops the run with its line number and exit code 2. A clean report is the only
exit 0.

A webhook that Esri switched off is the other trap. Its last delivery may have worked yesterday,
and the ledger still reads fresh. The server state wins: `OFF` outranks a recent success. The same
goes for a notebook that has failed five times since a success this morning: `STOPPED` outranks
the day count.

## Esri behaviour this relies on

Every page below was fetched on 2026-10-13.

- [Manage scheduled tasks](https://doc.esri.com/en/arcgis-notebooks/latest/use/manage-scheduled-tasks.html)
  (ArcGIS Notebooks). "If a task fails five times, it is stopped." The list shows Name, Owner, Next
  run, Created, Updated and Status, and no last-run field. The page does not say whether the five
  failures must be consecutive. The tool counts failures after the last success, which is the
  safe reading of either rule.
- [tasks: All User Scheduled Tasks](https://developers.arcgis.com/rest/users-groups-and-items/all-user-scheduled-tasks/).
  `GET https://[root]/community/users/[userName]/tasks`, introduced at ArcGIS Enterprise 10.8.1.
  The sample task holds `taskState`, `active`, `lastStart` and `updated`. The `taskState` filter
  lists active, completed, disabled, failed and paused. The page has no last-success field, and
  `lastStart` is a start time. The tool does not call this resource.
- [ArcGIS API for Python, tasks module](https://developers.arcgis.com/python/latest/api-reference/arcgis.gis.tasks.html).
  A task's `runs` property "returns all runs for the task", at most 30, as Run objects. The status
  of a run can be scheduled, executing, succeeded, failed or skipped. The REST page for the runs
  resource did not load on that date, so this README does not cite its fields, and the tool does
  not call it.
- [Webhooks (ArcGIS Server administrator API)](https://developers.arcgis.com/rest/enterprise-administration/server/webhooks/).
  `GET https://[root]/services/<serviceName>.<serviceType>/webhooks`, introduced at 11.1. The
  response holds a `webhooks` array. Each entry has `id`, `name` and a boolean `active`, "including
  deactivated and activated webhooks". The page lists no last-success or last-delivery property.
- [Webhook](https://developers.arcgis.com/rest/enterprise-administration/server/webhook/). `active`
  is a true or false flag for whether the webhook is enabled. The page does not say why a
  webhook is off.
- [Notification Status](https://developers.arcgis.com/rest/enterprise-administration/server/notification-status/).
  `GET .../webhooks/<webhookID>/notificationStatus`, introduced at 11.2, parameter `f`. It "returns a
  list of timestamp information for when an attempt to deliver a payload has failed". "When the
  maximum number of allowed failures is met, the webhook will be deactivated." The page gives no
  number for that maximum. The sample response is
  `{"webhookNotificationStatus": [{"invoked": false, "invokedTimestamp": 1695756823000}]}`.
  The page does not define `invoked` and does not state the unit of the timestamp.
- [HTTP authorization headers](https://developers.arcgis.com/documentation/security-and-authentication/reference/http-authorization-headers/).
  `X-Esri-Authorization: Bearer <token>` is a supported header, and ArcGIS Server supports header
  tokens from 10.5.1.

## What was exercised for real

| Claim | Where it ran |
|---|---|
| A job with no record is `NEVER`, and a file typo is exit 2 | self-test, `judge` and `run` assertions |
| The limit is exact: the limit itself is `OK`, one second over is `STALLED` | self-test, two boundary assertions |
| Five failures after the last success make a notebook `STOPPED`, even when the success was 6 hours ago | self-test, and a real run of an hourly job (one success, five failures, limit 2 days) that gave `STOPPED` and exit 1 |
| A webhook with five recorded failures gets no five-failure verdict or note | self-test |
| `--record` writes nothing without `--apply`; `--ap` is refused | self-test: ledger file absent after both; real run exit 0 and 64 |
| A torn or odd ledger line is refused with its line number | self-test: torn, extra key, bad boolean, bad name, bad time |
| A ledger nested 100000 levels deep is refused with exit 2 | self-test, and a real run on a 100000 bracket file |
| A ledger with no final newline is repaired on the next append | self-test, read back through the parser |
| 12 shell loops of 40 `--record --apply` runs each lose no line | real run on Windows 3.13 and on Linux 3.12: 480 lines each. The version before the lock kept 479 of 480 in the same Windows loop |
| 6 threads appending at once lose no line; a stale lock is removed; a fresh lock blocks the append | self-test |
| `OFF` outranks a recent success | self-test, and the `webhook:c3d4` row above |
| The token is in the header, not the URL, and not in any output | self-test against a stub that records headers; real run, `grep -c` found 0 hits in the report, stderr and ledger |
| A server that echoes the token in an error gets `***` instead | self-test, and a real run against an echoing stub: stderr read `Invalid token: Bearer ***` |
| An `http` proxy variable does not receive the request or the token | self-test with a proxy stub, and a real run with `HTTP_PROXY` set: the proxy stub saw 0 requests |
| A webhook name outside ASCII prints as escapes with output redirected | self-test with an ASCII-only stdout, and a real redirected run |
| A redirect is refused and the target is never contacted | self-test: the stub served the redirect, and no request reached the target path on the same stub |
| An error object, HTML, a JSON list, HTTP 404 and 500, a malformed reply, a huge body and a refused connection are all refused | self-test, one assertion each, against the local stub |
| The same 186 assertions pass on Windows and Linux, on Python 3.9, 3.12 and 3.13 | Windows 3.9.25 and 3.13.2, Linux 3.9.25 and 3.12.3, identical output |

## Limits

- It was never run against a live ArcGIS Enterprise site. The network path runs against a local
  stub that follows the documented response shape. A real site may differ: the admin port, a web
  adaptor path, a token with the right privilege, a certificate chain. The self-test never opens an
  https connection, so TLS is untested. The tool leaves the Python defaults in place and has no
  `--insecure` flag.
- Notebook state is not read from the server. The tool does not poll the tasks or runs resources.
  The ledger is the only source for a notebook, so a notebook that dies before its last step
  leaves no failure line, and only the day count then shows it. A notebook that Esri stopped
  shows `STOPPED` only if the job recorded its own five failures.
- The five-failure stop applies to notebook tasks only. Esri gives no number for a webhook, so a
  webhook row never gets `STOPPED` or the stop note.
- Only service webhooks are read (feature and geoprocessing services). Organization webhooks are
  not.
- The `invoked` value is not interpreted. The tool counts the entries in `webhookNotificationStatus`
  as logged failures, as the page describes, and shows the latest `invokedTimestamp`. The count
  includes entries that are not objects, so it can read high. The tool treats the timestamp as
  epoch milliseconds, which the page does not state, and a value in seconds shows as a 1970 date.
  A timestamp it cannot read is left out of the display. A timestamp before 1970 may show as a date
  on Linux and be left out on Windows. None of this changes a verdict.
- A webhook is `OK` when its last recorded success is inside the limit, even when the server's
  failure log shows a newer failed delivery. Esri deactivates a webhook at its maximum number of
  failures, and then the row reads `OFF`.
- Every webhook shows `NEVER` until its receiver has recorded one success.
- A job missing from `--expect` is judged with `--default-days` (2), so a weekly job without
  `--expect` reads `STALLED`.
- The DAYS column is rounded to one decimal, so a `STALLED` row can show a value equal to its
  limit, such as 2.0 for a limit of 2.
- A ledger is only as honest as the job that writes it. A job that records `ok` before its work ends
  hides a failure. Put `--record` last.
- The lock works between runs of this tool only. A program that writes the ledger another way is
  not covered. Two runs that find the same stale lock together could remove each other's new lock.
  The lock was tested on a local disk, not on a network share.
- A ledger written with a byte-order mark or in UTF-16 (PowerShell 5.1 redirection or
  `Set-Content -Encoding UTF8`) is refused with exit 2 and an ascii-decode message. Write the
  ledger only through `--record --apply`.
- A ledger line with a repeated key, such as two `job` keys, is accepted, and the last value wins.
  A form feed or other line-break control character inside a hand-edited line splits it, and the
  halves are refused as not JSON.
- If `--expect` names a job twice, as in `--expect a=0.01 --expect a=100`, the last limit wins
  without a warning. `--expect` also accepts what Python's `float()` accepts, such as fullwidth
  digits and `1_0`.
- `--outcome` and `--when` are ignored when `--record` is absent, so the run is only a report.
- Two webhooks that share one id collapse to a single entry, and the later one wins, so an `OFF`
  webhook can be hidden.
- A webhook that the server no longer lists, but that the ledger still names, is judged from the
  ledger alone.
- The tool uses the proxy settings of the environment for `https`. It never uses an `http` proxy,
  because plain `http` is allowed to the local machine only. Only a verbatim copy of the token in
  a server message is replaced. A server's own error text is shown, up to 100 characters, with
  control and non-ASCII characters escaped.
- The tool was run on Python 3.9.25, 3.12 and 3.13.2 only. The self-test needs free local ports,
  and it fails with "Only one usage of each socket address" on a Windows host that has just run
  tens of thousands of short connections.
- Coverage is 100 percent of lines and branches on Windows 3.13 and Linux 3.12. One line carries
  `# pragma: no branch`: the `if __name__ == "__main__"` guard. When coverage runs the file as a
  script, the guard can only be true.
- The webhook port and path in the examples are placeholders.

## Contributing

Open an issue or pull request on GitHub.

## Author

Built by [Asir Khan](https://www.linkedin.com/in/asir-khan-310317264/).

## License

MIT.

## Related

Other single-file tools in this portfolio that pair with this one:

- [taskpulse](https://github.com/uhsear/taskpulse) - the same question for a Windows scheduled task: did the last run work, and what will break the next
- [jobharness](https://github.com/uhsear/jobharness) - a run lock, checkpoints and retry for the job that writes the ledger
- [sharewatch](https://github.com/uhsear/sharewatch) - a read-only daily watcher for an ArcGIS organization, which diffs its sharing against yesterday
