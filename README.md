# stalled

Count the days since the last success of a scheduled notebook or a webhook, and say so when the count is too high.

Here is how a scheduled job dies without a sound. A scheduled notebook refreshes a layer every
night. One night it starts to fail, because a password changed or an input moved. The Esri
documentation says a task that fails five times is stopped. Nobody gets a page when that
happens. The task list shows a status, and no one opens the task list. A webhook goes the same
way. Its receiver is down for a while, the deliveries fail, and the server deactivates the
webhook. The layer behind the map goes stale. Weeks later a user asks why the map is old. The
first question is "when did it last work?", and Esri's documented REST API does not answer it.
The task list shows no last-run column. The webhook resource shows `active`, and its failure log
shows failures, never a success.

stalled answers the question with its own record. The job writes one line to a ledger when it
ends. A daily check reads the ledger, counts the days since the last success, and exits non-zero
when the count is over the limit. For a service webhook, the check also reads the server and
reports a webhook that is switched off, even when its last success was yesterday.

```console
$ python stalled.py --ledger runs.jsonl --webhooks-url http://127.0.0.1:18765/arcgis/admin/services/S.FeatureServer/webhooks --with-failures --expect nightly-refresh=1.5 --expect weekly-export=8 --now 2026-10-13T12:00:00Z
stalled report as of 2026-10-13T12:00:00Z
JOB                            VERDICT      DAYS  LAST SUCCESS         NOTE
nightly-refresh                OK            1.4  2026-10-12T03:00:00Z
webhook:a1b2                   OK            0.1  2026-10-13T09:00:00Z (parcel-sync) 1 logged failure(s), latest 2025-10-09T08:53:20Z
webhook:c3d4                   OFF           7.1  2026-10-06T09:00:00Z (permit-feed) 1 logged failure(s), latest 2025-10-09T08:53:20Z
weekly-export                  STALLED      11.2  2026-10-02T06:00:00Z 5 failures since the last success. A scheduled notebook task is stopped after 5.
4 job(s): 2 OK, 1 STALLED, 1 OFF
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
PASS  a job with no record is NEVER, not OK  <-- pinned defect
PASS  a success 6 minutes ahead is FUTURE  <-- pinned defect
PASS  a recent success does not hide an OFF webhook  <-- pinned defect
...
PASS  a unique prefix --ap of --apply is REFUSED by the parser  <-- pinned defect
...
PASS  a redirect is REFUSED and not followed  <-- pinned defect
...
--------------------------------------------------------------------
151 assertions, 0 failed
```

The run gives 151 assertions on Windows Python 3.13 and on Linux Python 3.12. The two outputs are
identical line for line. Branch coverage is 100 percent (see "Limits" for the one pragma).

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

For a webhook, run the same command from the receiver, with the job name `webhook:<id>`. The id is
the `id` that the server lists for that webhook.

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

Verdicts: `OK`, `STALLED` (older than the limit), `NEVER` (no success on record), `OFF` (the server
lists the webhook as not active) and `FUTURE` (a success dated more than 5 minutes after now).
Only `OK` is good.

Exit codes: 0 every job is `OK`. 1 at least one is not. 2 the tool could not decide: the ledger is
missing or torn, the read failed, nothing was there to judge, or the write failed. 64 usage error,
including an unknown flag.

The ledger line is `{"job": "nightly-refresh", "ok": true, "ts": "2026-10-13T03:00:00Z"}`. The time
is UTC with a `Z`. A time with no zone is refused, because it could be local time.

The webhook token comes from the `STALLED_TOKEN` environment variable only. There is no flag for it,
so it cannot land in a shell history or a process list. The tool sends it in the
`X-Esri-Authorization` header and never in a URL. It refuses a URL that holds a query string, a
login or a fragment. It refuses plain `http` to any host except the local machine. It does not
follow a redirect, because the header would go to the new host.

## Why the obvious version is wrong

The first version people write checks the last line of a log. That fails three ways. A job that
dies before it writes anything leaves an old success line, which reads as healthy for as long as
the log is kept. A ledger with a torn last line is easy to skip, and the skipped line is often the
failure. A job that has never run has no line at all, and "no data" prints as nothing.

stalled treats each of these as a finding. A job with no success is `NEVER`, and it exits 1. A
torn ledger line stops the run with its line number and exit code 2. A clean report is the only
exit 0.

A webhook that Esri switched off is the other trap. Its last delivery may have worked yesterday,
and the ledger still reads fresh. The server state wins: `OFF` outranks a recent success.

## Esri behaviour this relies on

Every page below was fetched on 2026-10-13.

- [Manage scheduled tasks](https://doc.esri.com/en/arcgis-notebooks/latest/use/manage-scheduled-tasks.html)
  (ArcGIS Notebooks). "If a task fails five times, it is stopped." The list shows Name, Owner, Next
  run, Created, Updated and Status, and no last-run field. The page does not say whether the five
  failures must be consecutive. The tool counts failures after the last success, which is the
  safe reading of either rule.
- [Notebook Server Administrator API resource hierarchy](https://developers.arcgis.com/rest/enterprise-administration/notebook/resource-hierarchy/).
  The page lists Notebooks, Logs and System (including Jobs). It lists no tasks resource. That is
  why the tool reads no notebook state from the server and uses the ledger alone for notebooks.
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
  maximum number of allowed failures is met, the webhook will be deactivated." The sample response
  is `{"webhookNotificationStatus": [{"invoked": false, "invokedTimestamp": 1695756823000}]}`.
  The page does not define `invoked` and does not state the unit of the timestamp.
- [HTTP authorization headers](https://developers.arcgis.com/documentation/security-and-authentication/reference/http-authorization-headers/).
  `X-Esri-Authorization: Bearer <token>` is a supported header, and ArcGIS Server supports header
  tokens from 10.5.1.

## What was exercised for real

| Claim | Where it ran |
|---|---|
| A job with no record is `NEVER`, and a file typo is exit 2 | self-test, `judge` and `run` assertions |
| The limit is exact: the limit itself is `OK`, one second over is `STALLED` | self-test, two boundary assertions |
| Five failures after the last success add the Esri stop note | self-test, and the `weekly-export` row above |
| `--record` writes nothing without `--apply`; `--ap` is refused | self-test: ledger file absent after both; real run exit 0 and 64 |
| A torn or odd ledger line is refused with its line number | self-test: torn, extra key, bad boolean, bad name, bad time |
| A ledger with no final newline is repaired on the next append | self-test, read back through the parser |
| `OFF` outranks a recent success | self-test, and the `webhook:c3d4` row above |
| The token is in the header, not the URL, and not in any output | self-test against a stub that records headers; real run, `grep -c` found 0 hits in the report, stderr and ledger |
| A redirect is refused and the target is never contacted | self-test: the stub served the redirect, and no request reached the target path on the same stub |
| An error object, HTML, a JSON list, HTTP 404 and 500, a malformed reply, a huge body and a refused connection are all refused | self-test, one assertion each, against the local stub |
| The same 151 assertions pass on Windows and Linux | Windows Python 3.13 and Linux Python 3.12, identical output |

## Limits

- It was never run against a live ArcGIS Enterprise site. The network path runs against a local
  stub that follows the documented response shape. A real site may differ: the admin port, a web
  adaptor path, a token with the right privilege, a certificate chain. The self-test never opens an
  https connection, so TLS is untested. The tool leaves the Python defaults in place and has no
  `--insecure` flag.
- Notebook state is not read from the server, because the Notebook Server administrator API
  reference lists no tasks resource. The ledger is the only source for a notebook. So a notebook
  that Esri stopped shows up as the days since its last success, not as `OFF`.
- Only service webhooks are read (feature and geoprocessing services). Organization webhooks are
  not.
- The `invoked` value is not interpreted. The tool counts the entries in `webhookNotificationStatus`
  as logged failures, as the page describes, and shows the latest `invokedTimestamp`. It treats
  that timestamp as epoch milliseconds, which the page does not state. A timestamp it cannot read
  is left out of the display. A failure count never changes a verdict.
- A ledger is only as honest as the job that writes it. A job that records `ok` before its work ends
  hides a failure. Put `--record` last.
- Appends are not locked. One short line per append is safe on a local disk. Two writers on a
  network share could interleave, and the next read would then refuse the ledger.
- A webhook that the server no longer lists, but that the ledger still names, is judged from the
  ledger alone.
- The tool uses the proxy settings of the environment. The self-test bypasses them for its
  loopback stub. If `HTTP_PROXY` is set, a real run of `--webhooks-url http://127.0.0.1...` goes to
  the proxy.
- Coverage is 100 percent of lines and branches. One line carries `# pragma: no branch`: the
  `if __name__ == "__main__"` guard. When coverage runs the file as a script, the guard can only be
  true.
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
