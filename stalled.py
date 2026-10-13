#!/usr/bin/env python
"""Count the days since the last success of a scheduled notebook or webhook.

ArcGIS switches off a scheduled notebook task after repeated failures, and it
deactivates a webhook the same way. Neither event reaches anybody who is not
looking at the right page, and neither REST API documents a "last success"
time. So the tool keeps its own record: a ledger file with one line per run.
A job that ends calls this tool with --record, and a daily check reads the
ledger and counts the days.

    python stalled.py --self-test
    python stalled.py --ledger runs.jsonl --expect nightly-refresh=1.5
    python stalled.py --ledger runs.jsonl --record nightly-refresh --apply
    python stalled.py --ledger runs.jsonl --record nightly-refresh --outcome fail --apply
    python stalled.py --ledger runs.jsonl --webhooks-url https://HOST/arcgis/admin/services/S.FeatureServer/webhooks

Verdicts: OK, STALLED (older than the limit), NEVER (no success on record),
OFF (a webhook the server lists as not active), FUTURE (a success dated after
now). Only OK is good. A job with no record is NEVER, not OK.

The only write is --record, and it needs --apply. It appends one line.

The webhook token comes from the STALLED_TOKEN environment variable only. It
goes in the X-Esri-Authorization header, never in a URL, and is never printed.

Exit codes: 0 every job is OK, 1 at least one is not, 2 the tool could not
decide (bad ledger, failed read, nothing to judge, failed write), 64 usage.
"""

from __future__ import print_function

import argparse
import collections
import contextlib
import datetime
import http.client
import io
import json
import math
import os
import re
import sys
import tempfile
import threading
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer

# =============================================================================
# CONFIGURATION. Deliberately not flags. Change here, not at the call site.
# =============================================================================

# A job with no --expect is judged against this many days.
DEFAULT_MAX_DAYS = 2.0

# Esri stops a scheduled notebook task after five failures. The report says so
# when the ledger shows that many failures after the last success.
FAIL_STOP = 5

# A success may be dated this far after "now" before it counts as FUTURE. It
# absorbs a small clock difference between the job host and the checking host.
FUTURE_SLACK_SECONDS = 300

TOKEN_ENV = "STALLED_TOKEN"
REQUEST_TIMEOUT = 30
MAX_RESPONSE_BYTES = 5 * 1024 * 1024
LOOPBACK = ("127.0.0.1", "::1", "localhost")

UTC = datetime.timezone.utc
STAMP_RE = re.compile(r"([0-9]{4})-([0-9]{2})-([0-9]{2})T"
                      r"([0-9]{2}):([0-9]{2}):([0-9]{2})Z\Z")
NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,79}\Z")

OK, STALLED, NEVER, OFF, FUTURE = "OK", "STALLED", "NEVER", "OFF", "FUTURE"

Row = collections.namedtuple(
    "Row", "name verdict days last streak limit note")


# ------------------------------------------------------------------ decision

def clean(value):
    """Text with every unprintable character escaped.

    A name from a server reaches stdout. A newline in it could forge a clean
    verdict line, and an ANSI escape could hide the real one.
    """
    text = str(value if value is not None else "").strip()
    return text if text.isprintable() else (
        text.encode("unicode_escape").decode("ascii"))


def parse_stamp(text):
    """A UTC time written exactly as 2026-10-13T03:00:00Z. Raises ValueError.

    A time with no zone is refused: it could be local time, and a job that
    runs at 23:00 local would be a day off.
    """
    match = STAMP_RE.match(text) if isinstance(text, str) else None
    if not match:
        raise ValueError("not a UTC time written like 2026-10-13T03:00:00Z: "
                         "%s" % clean(text)[:40])
    return datetime.datetime(*[int(g) for g in match.groups()], tzinfo=UTC)


def format_stamp(moment):
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def check_name(name):
    """A job name is ASCII with no space, so it cannot forge a report line."""
    if not isinstance(name, str) or not NAME_RE.match(name):
        raise ValueError("a job name is 1 to 80 characters from A-Z a-z 0-9 "
                         ". _ : -, starting with a letter or digit: %s"
                         % clean(name)[:40])
    return name


def parse_expect(spec):
    """NAME=DAYS into (name, days). Days is a finite number above zero."""
    name, sep, days = spec.partition("=")
    if not sep:
        raise ValueError("--expect needs NAME=DAYS: %s" % clean(spec)[:40])
    check_name(name)
    try:
        value = float(days)
    except ValueError:
        raise ValueError("the days in --expect are not a number: %s"
                         % clean(days)[:40])
    if not math.isfinite(value) or value <= 0 or value > 36500:
        raise ValueError("the days in --expect must be above 0 and at most "
                         "36500: %s" % clean(days)[:40])
    return name, value


def parse_ledger(text):
    """The ledger text into {name: [(time, ok)]}. Raises ValueError.

    One JSON object per line: {"job": NAME, "ts": UTC time, "ok": true}. A
    line that does not fit is refused with its number. Skipping it would hide
    the failure that tore it.
    """
    events = {}
    for number, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except ValueError:
            raise ValueError("ledger line %d is not JSON." % number)
        if not isinstance(item, dict) or set(item) != set(("job", "ts", "ok")):
            raise ValueError("ledger line %d must hold exactly job, ts and "
                             "ok." % number)
        if not isinstance(item["ok"], bool):
            raise ValueError("ledger line %d: ok must be true or false."
                             % number)
        try:
            name = check_name(item["job"])
            moment = parse_stamp(item["ts"])
        except ValueError as exc:
            raise ValueError("ledger line %d: %s" % (number, exc))
        events.setdefault(name, []).append((moment, item["ok"]))
    return events


def judge(name, events, now, limit, state=None):
    """One job's Row. Pure: the same inputs give the same verdict."""
    wins = [t for t, ok in events if ok]
    last = max(wins) if wins else None
    streak = len([t for t, ok in events if not ok and (last is None or t > last)])
    days = None if last is None else (now - last).total_seconds() / 86400.0
    note = ""
    if streak >= FAIL_STOP:
        note = ("%d failures since the last success. A scheduled notebook "
                "task is stopped after %d." % (streak, FAIL_STOP))
    if state == "off":
        verdict = OFF
    elif last is None:
        verdict = NEVER
    elif days * 86400.0 < -FUTURE_SLACK_SECONDS:
        verdict = FUTURE
    elif days > limit:
        verdict = STALLED
    else:
        verdict = OK
    return Row(name, verdict, days, last, streak, limit, note)


def exit_code(rows):
    return 0 if rows and all(r.verdict == OK for r in rows) else 1


def render(rows, now, extra=None):
    """The report as a list of lines."""
    extra = extra or {}
    out = ["stalled report as of %s" % format_stamp(now),
           "%-30s %-8s %8s  %-20s %s" % ("JOB", "VERDICT", "DAYS",
                                          "LAST SUCCESS", "NOTE")]
    for r in rows:
        days = "-" if r.days is None else "%.1f" % r.days
        if days == "-0.0":  # a success inside the clock slack
            days = "0.0"
        last = "-" if r.last is None else format_stamp(r.last)
        notes = [n for n in (extra.get(r.name), r.note) if n]
        out.append(("%-30s %-8s %8s  %-20s %s"
                    % (r.name, r.verdict, days, last, " ".join(notes))).rstrip())
    counts = collections.Counter(r.verdict for r in rows)
    out.append("%d job(s): %s" % (len(rows), ", ".join(
        "%d %s" % (counts[v], v)
        for v in (OK, STALLED, NEVER, OFF, FUTURE) if counts[v])))
    return out


# ------------------------------------------------------------------- ledger

def read_ledger(path):
    try:
        with open(path, "rb") as fh:
            raw = fh.read()
        text = raw.decode("ascii")
    except (OSError, UnicodeDecodeError) as exc:
        raise ValueError("cannot read the ledger: %s" % clean(exc)[:100])
    return parse_ledger(text)


def record_line(name, moment, ok):
    return json.dumps({"job": check_name(name), "ts": format_stamp(moment),
                       "ok": bool(ok)}, sort_keys=True)


def append_line(path, line):
    """Append one line. A file that lacks a final newline gets one first."""
    with open(path, "ab+") as fh:
        fh.seek(0, 2)
        lead = b""
        if fh.tell():
            fh.seek(-1, 2)
            lead = b"" if fh.read(1) == b"\n" else b"\n"
        fh.write(lead + line.encode("ascii") + b"\n")


# ------------------------------------------------------------------ network

def holds_secret(url):
    p = urllib.parse.urlsplit(url)
    return bool(p.query or p.fragment or p.username or p.password)


def check_url(url):
    """Raise unless the URL is one this tool may send a token to.

    https, or http to this machine only. A query string, a fragment or a login
    in the URL may hold a credential, so it is refused without being echoed.
    """
    p = urllib.parse.urlsplit(url)
    if holds_secret(url):
        raise ValueError("a URL must hold no query string, fragment or "
                         "login. The token goes in %s." % TOKEN_ENV)
    if p.scheme == "http" and p.hostname in LOOPBACK:
        return
    if p.scheme != "https" or not p.hostname:
        raise ValueError("a URL must be https, or http to the local "
                         "machine.")


def check_token(token):
    """The token from the environment, trimmed. Refuse one that is malformed."""
    token = token.strip()
    if not token.isascii() or not token.isprintable() or " " in token:
        raise ValueError("%s holds a space, a control character or a "
                         "character outside ASCII." % TOKEN_ENV)
    return token


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse a redirect: urllib would resend the token header to its target."""

    def redirect_request(self, *args, **kwargs):
        return None


OPENER = urllib.request.build_opener(NoRedirect)


def http_json(url, token, opener, params, max_bytes=MAX_RESPONSE_BYTES,
              timeout=REQUEST_TIMEOUT):
    """One GET. Returns the parsed JSON object, or raises ValueError."""
    check_url(url)
    req = urllib.request.Request(url + "?" + urllib.parse.urlencode(params))
    if token:
        req.add_header("X-Esri-Authorization", "Bearer " + token)
    try:
        with opener.open(req, timeout=timeout) as resp:
            raw = resp.read(max_bytes + 1)
    except (OSError, http.client.HTTPException) as exc:
        raise ValueError("the request failed: %s" % clean(exc)[:120])
    if len(raw) > max_bytes:
        raise ValueError("the response is larger than %d bytes." % max_bytes)
    try:
        data = json.loads(raw.decode("utf-8"))
    except ValueError:
        raise ValueError("the response is not JSON.")
    if not isinstance(data, dict):
        raise ValueError("the response is not a JSON object.")
    if "error" in data:
        err = data["error"] if isinstance(data["error"], dict) else {}
        raise ValueError("the service returned an error: code %s, %s"
                         % (clean(err.get("code")),
                            clean(err.get("message"))[:100]))
    return data


def stamp_from_millis(value):
    """An epoch-millisecond number into a stamp, or None when unreadable."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        return format_stamp(datetime.datetime.fromtimestamp(value / 1000.0, UTC))
    except (ValueError, OverflowError, OSError):
        return None


def probe_webhooks(url, token, opener, with_failures=False, **kw):
    """The service webhooks as {key: {"name", "state", "info"}}.

    key is "webhook:" plus the webhook id, so a rename does not lose the
    ledger. state is "off" when the server lists active as false. A webhook
    with no boolean active is refused: it is not guessed to be on.
    """
    path = url.rstrip("/")
    if not path.endswith("/webhooks"):
        raise ValueError("the URL must end in /webhooks.")
    data = http_json(path, token, opener, {"f": "json"}, **kw)
    hooks = data.get("webhooks")
    if not isinstance(hooks, list):
        raise ValueError("the response has no webhooks list.")
    found = {}
    for hook in hooks:
        if not isinstance(hook, dict) or not isinstance(hook.get("active"), bool):
            raise ValueError("a webhook has no true or false active value.")
        key = "webhook:" + check_name(hook.get("id"))
        info = ""
        if with_failures:
            status = http_json(path + "/" + hook["id"] + "/notificationStatus",
                               token, opener, {"f": "json"}, **kw)
            entries = status.get("webhookNotificationStatus")
            if not isinstance(entries, list):
                raise ValueError("the notification status has no list.")
            stamps = [stamp_from_millis(e.get("invokedTimestamp"))
                      for e in entries if isinstance(e, dict)]
            stamps = sorted(s for s in stamps if s)
            info = "%d logged failure(s)" % len(entries)
            if stamps:
                info += ", latest %s" % stamps[-1]
        found[key] = {"name": clean(hook.get("name"))[:40],
                      "state": None if hook["active"] else "off",
                      "info": info}
    return found


# ---------------------------------------------------------------------- run

def report(args, now, opener):
    """The report path. Returns the exit code."""
    try:
        events = read_ledger(args.ledger)
        limits = dict(args.expect)
        hooks = {}
        if args.webhooks_url:
            token = os.environ.get(TOKEN_ENV, "")
            hooks = probe_webhooks(args.webhooks_url,
                                   check_token(token) if token else "",
                                   opener, args.with_failures)
    except ValueError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 2
    names = sorted(set(events) | set(limits) | set(hooks))
    if not names:
        print("error: nothing to judge. The ledger is empty and no --expect "
              "or webhook was given.", file=sys.stderr)
        return 2
    extra = {}
    rows = []
    for name in names:
        hook = hooks.get(name, {})
        if hook:
            extra[name] = " ".join(
                s for s in ("(%s)" % hook["name"], hook["info"]) if s)
        rows.append(judge(name, events.get(name, []), now,
                          limits.get(name, args.default_days),
                          hook.get("state")))
    for line in render(rows, now, extra):
        print(line)
    return exit_code(rows)


def record(args, now):
    """The --record path. Returns the exit code."""
    when = parse_stamp(args.when) if args.when else now
    if when > now + datetime.timedelta(seconds=FUTURE_SLACK_SECONDS):
        raise ValueError("--when is in the future.")
    line = record_line(args.record, when, args.outcome == "ok")
    if not args.apply:
        print("check only. Would append to %s: %s" % (args.ledger, line))
        print("Re-run with --apply to write it.")
        return 0
    try:
        append_line(args.ledger, line)
    except OSError as exc:
        print("error: cannot write the ledger: %s" % clean(exc)[:100],
              file=sys.stderr)
        return 2
    print("recorded: %s" % line)
    return 0


class Parser(argparse.ArgumentParser):
    """argparse exits 2 on a bad flag. 2 means "could not decide" here."""

    def error(self, message):
        self.print_usage(sys.stderr)
        self.exit(64, "%s: error: %s\n" % (self.prog, message))


def _parse(argv):
    ap = Parser(
        prog="stalled.py",
        allow_abbrev=False,
        description="Count the days since the last success of a scheduled "
                    "notebook or webhook.",
        epilog="Nothing is written without --apply. The webhook token comes "
               "from the %s environment variable." % TOKEN_ENV)
    ap.add_argument("--ledger", help="the run ledger, one JSON line per run")
    ap.add_argument("--expect", action="append", default=[],
                    metavar="NAME=DAYS",
                    help="a job and the most days it may go without a "
                         "success. Repeat for more jobs.")
    ap.add_argument("--default-days", dest="default_days", type=float,
                    default=DEFAULT_MAX_DAYS,
                    help="the limit for a job with no --expect (default %g)"
                         % DEFAULT_MAX_DAYS)
    ap.add_argument("--now", help="judge as of this UTC time, "
                                  "2026-10-13T03:00:00Z (default: now)")
    ap.add_argument("--webhooks-url",
                    help="a service webhooks URL ending in /webhooks. "
                         "Read only.")
    ap.add_argument("--with-failures", action="store_true",
                    help="also read each webhook's notificationStatus")
    ap.add_argument("--record", metavar="NAME",
                    help="append a run of this job to the ledger")
    ap.add_argument("--outcome", choices=("ok", "fail"), default="ok",
                    help="the outcome --record writes (default ok)")
    ap.add_argument("--when", help="the UTC time --record writes "
                                   "(default: now)")
    ap.add_argument("--apply", action="store_true",
                    help="write the --record line. Without it nothing is "
                         "written.")
    ap.add_argument("--self-test", action="store_true", dest="self_test",
                    help="run the offline assertions and exit")
    return ap.parse_args(argv)


def usage_error(message):
    print("error: %s" % message, file=sys.stderr)
    return 64


def main(argv=None, opener=None):
    args = _parse(sys.argv[1:] if argv is None else argv)
    if args.self_test:
        return self_test()
    if not args.ledger:
        return usage_error("--ledger is required. Use --self-test to check "
                           "the tool without one.")
    if args.apply and not args.record:
        return usage_error("--apply writes a --record line, and none was "
                           "given.")
    if args.record and (args.expect or args.webhooks_url):
        return usage_error("--record cannot be combined with --expect or "
                           "--webhooks-url.")
    if args.with_failures and not args.webhooks_url:
        return usage_error("--with-failures needs --webhooks-url.")
    if not math.isfinite(args.default_days) or args.default_days <= 0:
        return usage_error("--default-days must be above 0.")
    try:
        args.expect = [parse_expect(s) for s in args.expect]
        now = parse_stamp(args.now) if args.now else (
            datetime.datetime.now(UTC).replace(microsecond=0))
        if args.record:
            return record(args, now)
    except ValueError as exc:
        return usage_error(str(exc))
    return report(args, now, opener or OPENER)


# ----------------------------------------------------------------- self-test

class Harness(object):
    """The assertion counter behind --self-test. The self-test tests it too."""

    def __init__(self):
        self.passed = 0
        self.failed = []

    def check(self, cond, label):
        if cond:
            self.passed += 1
            print("PASS  %s" % label)
        else:
            self.failed.append(label)
            print("FAIL  %s" % label)

    def raises(self, fn, label, text=None):
        """Pass when fn raises ValueError, whose message holds text if given."""
        try:
            fn()
        except ValueError as exc:
            self.check(text is None or text in str(exc), label)
        except Exception as exc:
            self.check(False, "%s (wrong exception %r)" % (label, exc))
        else:
            self.check(False, "%s (no error raised)" % label)

    def finish(self):
        """Print the footer. Returns the exit code."""
        print("-" * 68)
        total = self.passed + len(self.failed)
        if self.failed:
            print("%d assertions, %d failed" % (total, len(self.failed)))
            for label in self.failed:
                print("  FAILED: %s" % label)
            return 1
        print("%d assertions, 0 failed" % total)
        return 0


def self_test():
    """Assertions over the core, the ledger, and the network path (a local stub)."""
    harness = Harness()
    check, raises = harness.check, harness.raises

    def stamp(text):
        return parse_stamp(text)

    NOW = stamp("2026-10-13T12:00:00Z")

    def ev(*items):
        return [(stamp(t), ok) for t, ok in items]

    print("stalled self-test: no ArcGIS, no credentials, loopback stub only")
    print("-" * 68)

    # ---- times
    check(stamp("2026-10-13T03:00:00Z") == datetime.datetime(
        2026, 10, 13, 3, 0, 0, tzinfo=UTC), "a UTC time with Z parses")
    check(format_stamp(stamp("2026-01-02T03:04:05Z")) == "2026-01-02T03:04:05Z",
          "a time formats back to the same text")
    raises(lambda: stamp("2026-10-13T03:00:00"),
           "a time with no zone is REFUSED  <-- pinned defect")
    raises(lambda: stamp("2026-10-13T03:00:00+00:00"), "an offset is refused")
    raises(lambda: stamp("2026-10-13T03:00:00Z\n"),
           "a trailing newline is refused  <-- pinned defect")
    raises(lambda: stamp("2026-13-13T03:00:00Z"), "month 13 is refused")
    raises(lambda: stamp("2026-10-13T03:00:60Z"), "second 60 is refused")
    raises(lambda: stamp(u"\u0662026-10-13T03:00:00Z"),
           "non-ASCII digits are refused  <-- pinned defect")
    raises(lambda: stamp(1760000000), "a number is not a time")
    check(clean("a\nb") == "a\\nb", "clean escapes a newline")
    check(clean(None) == "", "clean of None is empty")

    # ---- names and --expect
    check(check_name("nightly-refresh") == "nightly-refresh", "a plain name passes")
    check(check_name("webhook:ab12.c_d") == "webhook:ab12.c_d",
          "a webhook key passes")
    raises(lambda: check_name("a b"), "a name with a space is refused")
    raises(lambda: check_name("a\nOK"),
           "a name with a newline is refused  <-- pinned defect")
    raises(lambda: check_name(""), "an empty name is refused")
    raises(lambda: check_name("x" * 81), "an 81 character name is refused")
    raises(lambda: check_name(7), "a number is not a name")
    check(parse_expect("job-1=1.5") == ("job-1", 1.5), "--expect reads NAME=DAYS")
    raises(lambda: parse_expect("job-1"), "--expect with no = is refused")
    raises(lambda: parse_expect("job-1=soon"), "--expect days must be a number")
    raises(lambda: parse_expect("job-1=0"), "--expect days of 0 are refused")
    raises(lambda: parse_expect("job-1=-3"), "negative --expect days are refused")
    raises(lambda: parse_expect("job-1=nan"),
           "nan days are refused  <-- pinned defect")
    raises(lambda: parse_expect("job-1=inf"), "infinite days are refused")
    raises(lambda: parse_expect("job-1=40000"), "days above 36500 are refused")
    raises(lambda: parse_expect("a b=1"), "--expect checks the name too")

    # ---- the verdicts
    ok = judge("j", ev(("2026-10-13T00:00:00Z", True)), NOW, 2)
    check(ok.verdict == OK and ok.days == 0.5, "12 hours after a success is OK, 0.5 days")
    st = judge("j", ev(("2026-10-09T12:00:00Z", True)), NOW, 2)
    check(st.verdict == STALLED and st.days == 4.0, "4 days after a success is STALLED")
    check(judge("j", ev(("2026-10-11T12:00:00Z", True)), NOW, 2).verdict == OK,
          "exactly the limit is still OK")
    check(judge("j", ev(("2026-10-11T11:59:59Z", True)), NOW, 2).verdict == STALLED,
          "one second past the limit is STALLED")
    nv = judge("j", [], NOW, 2)
    check(nv.verdict == NEVER and nv.days is None and nv.last is None,
          "a job with no record is NEVER, not OK  <-- pinned defect")
    nf = judge("j", ev(("2026-10-13T00:00:00Z", False)), NOW, 2)
    check(nf.verdict == NEVER and nf.streak == 1,
          "a job with only failures is NEVER")
    check(judge("j", ev(("2026-10-13T12:03:00Z", True)), NOW, 2).verdict == OK,
          "a success 3 minutes ahead is within the clock slack")
    check(judge("j", ev(("2026-10-13T12:06:00Z", True)), NOW, 2).verdict == FUTURE,
          "a success 6 minutes ahead is FUTURE  <-- pinned defect")
    off = judge("j", ev(("2026-10-13T00:00:00Z", True)), NOW, 2, "off")
    check(off.verdict == OFF, "a recent success does not hide an OFF webhook"
          "  <-- pinned defect")
    check(judge("j", [], NOW, 2, "off").verdict == OFF,
          "OFF outranks NEVER")
    mixed = judge("j", ev(("2026-10-10T00:00:00Z", True),
                          ("2026-10-12T00:00:00Z", False),
                          ("2026-10-12T06:00:00Z", False)), NOW, 2)
    check(mixed.verdict == STALLED and mixed.streak == 2 and mixed.note == "",
          "two failures after the last success count, with no stop note")
    check(judge("j", ev(("2026-10-12T00:00:00Z", False),
                        ("2026-10-12T06:00:00Z", True)), NOW, 2).streak == 0,
          "a failure before the last success is not counted")
    five = judge("j", ev(*[("2026-10-1%dT00:00:00Z" % d, False)
                          for d in range(1, 6)]), NOW, 2)
    check(five.streak == 5 and "stopped after 5" in five.note,
          "five failures add the Esri stop note")
    check(judge("j", ev(("2026-10-12T00:00:00Z", True),
                        ("2026-10-08T00:00:00Z", True)), NOW, 2).days == 1.5,
          "the newest success counts, whatever the line order")
    check(exit_code([ok]) == 0, "all OK exits 0")
    check(exit_code([ok, st]) == 1, "one STALLED exits 1")
    check(exit_code([]) == 1, "no rows never exits 0")

    # ---- the report text
    lines = render([ok, st, nv], NOW, {"j": "(hook)"})
    check(lines[0] == "stalled report as of 2026-10-13T12:00:00Z",
          "the report names the time it judges")
    check(any(l.split()[:2] == ["j", "OK"] for l in lines), "the report has an OK row")
    check("-" in lines[4].split()[2:4] and "NEVER" in lines[4],
          "a NEVER row prints - for days and last success")
    check(lines[-1] == "3 job(s): 1 OK, 1 STALLED, 1 NEVER",
          "the summary counts each verdict")
    check("(hook)" in lines[2], "extra text is printed on the row")
    slack = render([judge("j", ev(("2026-10-13T12:01:00Z", True)), NOW, 2)], NOW)
    check(slack[2].split()[1:3] == ["OK", "0.0"],
          "a success inside the clock slack prints 0.0 days, not -0.0"
          "  <-- pinned defect")

    # ---- the ledger
    text = ('{"job": "a", "ts": "2026-10-12T00:00:00Z", "ok": true}\n'
            '\r\n'
            '{"job": "a", "ts": "2026-10-13T00:00:00Z", "ok": false}\r\n')
    led = parse_ledger(text)
    check(sorted(led) == ["a"] and len(led["a"]) == 2,
          "the ledger groups lines by job, skipping a blank line")
    check(parse_ledger("") == {}, "an empty ledger is empty")
    raises(lambda: parse_ledger('{"job": "a", "ts": "2026-10-12T00:00:00Z", '
                                '"ok": tr'),
           "a torn last line is REFUSED, not skipped  <-- pinned defect",
           "line 1 is not JSON")
    raises(lambda: parse_ledger("[1]\n"), "a line that is not an object is refused")
    raises(lambda: parse_ledger('{"job": "a"}\n'), "a line with a missing key is refused")
    raises(lambda: parse_ledger('{"job": "a", "ts": "2026-10-12T00:00:00Z", '
                                '"ok": true, "x": 1}\n'),
           "a line with an extra key is refused")
    raises(lambda: parse_ledger('{"job": "a", "ts": "2026-10-12T00:00:00Z", '
                                '"ok": "yes"}\n'),
           "ok must be a real boolean  <-- pinned defect", "ok must be")
    raises(lambda: parse_ledger('{"job": "a b", "ts": "2026-10-12T00:00:00Z", '
                                '"ok": true}\n'),
           "a bad name in the ledger names its line", "line 1:")
    raises(lambda: parse_ledger('\n{"job": "a", "ts": "yesterday", '
                                '"ok": true}\n'),
           "a bad time in the ledger names line 2", "line 2:")
    check(record_line("a", NOW, True) ==
          '{"job": "a", "ok": true, "ts": "2026-10-13T12:00:00Z"}',
          "a record line is fixed JSON")
    raises(lambda: record_line("a b", NOW, True), "a record line checks the name")

    work = tempfile.mkdtemp(prefix="stalled-")
    made = []

    def path(name):
        made.append(os.path.join(work, name))
        return made[-1]

    def wr(name, data):
        with open(path(name), "wb") as fh:
            fh.write(data)
        return made[-1]

    def run(argv, opener=None):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = main(argv, opener)
            except SystemExit as exc:
                code = exc.code
        return code, out.getvalue(), err.getvalue()

    try:
        # ---- append and read back
        led1 = path("one.jsonl")
        append_line(led1, record_line("a", NOW, True))
        append_line(led1, record_line("a", NOW, False))
        with open(led1, "rb") as fh:
            body = fh.read()
        check(body.count(b"\n") == 2 and len(read_ledger(led1)["a"]) == 2,
              "append writes one line each and the ledger reads back")
        led2 = wr("nonl.jsonl", record_line("a", NOW, True).encode("ascii"))
        append_line(led2, record_line("b", NOW, True))
        check(sorted(read_ledger(led2)) == ["a", "b"],
              "a ledger lacking a final newline is repaired, not glued"
              "  <-- pinned defect")
        raises(lambda: read_ledger(path("missing.jsonl")),
               "a missing ledger is an error", "cannot read the ledger")
        raises(lambda: read_ledger(work), "a directory is not a ledger")
        raises(lambda: read_ledger(wr("uni.jsonl", b"\xc3\xa9")),
               "a non-ASCII ledger is refused", "cannot read the ledger")
        check(not os.path.exists(path("never-made")),
              "reading never creates a file")

        # ---- the CLI: report
        good = wr("good.jsonl", (
            record_line("fresh", NOW - datetime.timedelta(hours=3), True) + "\n" +
            record_line("old", NOW - datetime.timedelta(days=9), True) + "\n" +
            "\n".join(record_line("sick", NOW - datetime.timedelta(hours=h),
                                  False) for h in range(1, 6)) + "\n"
            ).encode("ascii"))
        now_arg = ["--now", "2026-10-13T12:00:00Z"]
        code, out, err = run(["--ledger", good] + now_arg)
        check(code == 1 and "fresh" in out and "STALLED" in out
              and "NEVER" in out, "a mixed ledger exits 1 and names each verdict")
        check("5 failures since the last success" in out,
              "the report carries the five-failure note")
        code, out, err = run(["--ledger", good, "--expect", "old=30"] + now_arg)
        check(any(l.split()[:2] == ["old", "OK"] for l in out.splitlines()),
              "--expect widens the limit for one job")
        code, out, err = run(["--ledger", good, "--default-days", "30"] + now_arg)
        check(any(l.split()[:2] == ["old", "OK"] for l in out.splitlines()),
              "--default-days widens the limit for every job")
        code, out, err = run(["--ledger", good, "--expect", "ghost=1"] + now_arg)
        check(any(l.split()[:2] == ["ghost", "NEVER"] for l in out.splitlines()),
              "a job named only in --expect is NEVER")
        allgood = wr("allgood.jsonl",
                     (record_line("fresh", NOW, True) + "\n").encode("ascii"))
        code, out, err = run(["--ledger", allgood] + now_arg)
        check(code == 0 and out.splitlines()[-1] == "1 job(s): 1 OK",
              "an all-OK ledger exits 0")
        code, out, err = run(["--ledger", wr(
            "y2020.jsonl", (record_line("fresh", stamp("2020-01-01T00:00:00Z"),
                                        True) + "\n").encode("ascii"))])
        check(code == 1 and "STALLED" in out,
              "with no --now the check uses the real clock")
        code, out, err = run(["--ledger", wr("empty.jsonl", b"")] + now_arg)
        check(code == 2 and "nothing to judge" in err,
              "an empty ledger with no --expect exits 2  <-- pinned defect")
        code, out, err = run(["--ledger", path("missing.jsonl")] + now_arg)
        check(code == 2 and "cannot read the ledger" in err,
              "a missing ledger exits 2")
        code, out, err = run(["--ledger", wr("bad.jsonl", b"{oops\n")] + now_arg)
        check(code == 2 and "line 1" in err and out == "",
              "a torn ledger exits 2 and prints no verdict")

        # ---- the CLI: usage
        check(run([])[0] == 64, "no --ledger is a usage error")
        check(run(["--ledger", good, "--apply"])[0] == 64,
              "--apply without --record is a usage error")
        check(run(["--ledger", good, "--record", "a", "--expect", "a=1"])[0] == 64,
              "--record with --expect is a usage error")
        check(run(["--ledger", good, "--record", "a",
                   "--webhooks-url", "https://h/webhooks"])[0] == 64,
              "--record with --webhooks-url is a usage error")
        check(run(["--ledger", good, "--with-failures"])[0] == 64,
              "--with-failures without --webhooks-url is a usage error")
        check(run(["--ledger", good, "--default-days", "0"])[0] == 64,
              "--default-days 0 is a usage error")
        check(run(["--ledger", good, "--default-days", "nan"])[0] == 64,
              "--default-days nan is a usage error  <-- pinned defect")
        check(run(["--ledger", good, "--expect", "bad"])[0] == 64,
              "a bad --expect is a usage error")
        check(run(["--ledger", good, "--now", "tomorrow"])[0] == 64,
              "a bad --now is a usage error")
        code, out, err = run(["--ledger", good, "--record", "a b"])
        check(code == 64, "a bad --record name is a usage error")
        check(run(["--ledger", good, "--record", "a", "--when", "x"])[0] == 64,
              "a bad --when is a usage error")
        code, out, err = run(["--ledger", good, "--record", "a", "--when",
                              "2027-01-01T00:00:00Z"] + now_arg)
        check(code == 64 and "future" in err, "a --when in the future is refused")
        code, out, err = run(["--ledger", good, "--record", "a", "--when",
                              "2026-10-13T12:04:00Z"] + now_arg)
        check(code == 0 and "check only" in out,
              "a --when 4 minutes ahead is inside the slack")
        code, out, err = run(["--ledger", good, "--record", "a", "--when",
                              "2026-10-13T12:06:00Z"] + now_arg)
        check(code == 64 and "future" in err,
              "a --when 6 minutes ahead is refused")
        check(run(["--ledger", good, "--record", "a", "--outcome", "meh"])[0] == 64,
              "an unknown --outcome is refused by the parser")
        code, out, err = run(["--ledger", good, "--bogus"])
        check(code == 64 and "unrecognized" in err,
              "an unknown flag exits 64")

        # ---- the CLI: record, and the --apply gate
        rec = path("rec.jsonl")
        code, out, err = run(["--ledger", rec, "--record", "nightly"] + now_arg)
        check(code == 0 and "check only" in out and not os.path.exists(rec),
              "--record without --apply writes nothing  <-- pinned defect")
        code, out, err = run(["--ledger", rec, "--ap", "--record", "nightly"])
        check(code == 64 and not os.path.exists(rec),
              "a unique prefix --ap of --apply is REFUSED by the parser"
              "  <-- pinned defect")
        code, out, err = run(["--ledger", rec, "--record", "nightly",
                              "--apply"] + now_arg)
        check(code == 0 and "recorded" in out and os.path.exists(rec),
              "--record --apply appends a line")
        code, out, err = run(["--ledger", rec, "--record", "nightly",
                              "--outcome", "fail", "--when",
                              "2026-10-13T11:00:00Z", "--apply"] + now_arg)
        check(code == 0 and len(read_ledger(rec)["nightly"]) == 2,
              "--outcome fail and --when are written")
        code, out, err = run(["--ledger", rec] + now_arg)
        check(code == 0 and "nightly" in out,
              "a recorded success reads back as OK")
        code, out, err = run(["--ledger", work, "--record", "nightly", "--apply"]
                             + now_arg)
        check(code == 2 and "cannot write the ledger" in err,
              "a ledger that cannot be written exits 2")
        env = os.environ.pop(TOKEN_ENV, None)

        # ---- the network path, against a local stub
        seen = []
        hooks = {"webhooks": [
            {"id": "h1", "name": "alpha", "active": True},
            {"id": "h2", "name": "be\nta", "active": False}]}
        fails = {"webhookNotificationStatus": [
            {"invoked": False, "invokedTimestamp": 1760000000000},
            {"invoked": False, "invokedTimestamp": 1760100000000},
            {"invoked": False, "invokedTimestamp": 10 ** 30},
            {"invoked": False}, "junk"]}
        routes = {
            "/ok/webhooks": hooks,
            "/ok/webhooks/h1/notificationStatus": fails,
            "/ok/webhooks/h2/notificationStatus": {
                "webhookNotificationStatus": []},
            "/err/webhooks": {"error": {"code": 498, "message": "Invalid token."}},
            "/err2/webhooks": {"error": "bad"},
            "/list/webhooks": [1],
            "/none/webhooks": {"webhooks": "x"},
            "/noactive/webhooks": {"webhooks": [{"id": "h1"}]},
            "/nodict/webhooks": {"webhooks": [5]},
            "/badid/webhooks": {"webhooks": [{"id": "a b", "active": True}]},
            "/nostat/webhooks": {"webhooks": [{"id": "h1", "active": True}]},
            "/nostat/webhooks/h1/notificationStatus": {"x": 1},
            "/empty/webhooks": {"webhooks": []}}

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                parts = urllib.parse.urlsplit(self.path)
                seen.append({"path": parts.path, "query": parts.query,
                             "auth": self.headers.get("X-Esri-Authorization"),
                             "other": self.headers.get("Authorization")})
                if parts.path.startswith("/redir"):
                    self.send_response(302)
                    self.send_header("Location", "http://127.0.0.1:%d/landed"
                                     % self.server.server_address[1])
                    self.end_headers()
                    return
                if parts.path.startswith("/junk"):
                    self.wfile.write(b"junk\r\n\r\n")
                    return
                if parts.path.startswith("/http500"):
                    body, code = b"{}", 500
                elif parts.path.startswith("/notjson"):
                    body, code = b"<html>", 200
                elif parts.path.startswith("/big"):
                    body, code = b"{" + b" " * 5000 + b"}", 200
                elif parts.path in routes:
                    body, code = json.dumps(routes[parts.path]).encode(), 200
                else:
                    body, code = b"{}", 404
                self.send_response(code)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        server = HTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever)
        thread.daemon = True
        thread.start()
        base = "http://127.0.0.1:%d" % server.server_address[1]
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}),
                                             NoRedirect)
        secret = "tok-SECRET-12345"
        try:
            def probe(route, **kw):
                return probe_webhooks(base + route, secret, opener, **kw)

            found = probe("/ok/webhooks")
            check(sorted(found) == ["webhook:h1", "webhook:h2"],
                  "the probe keys each webhook by id")
            check(found["webhook:h1"]["state"] is None
                  and found["webhook:h2"]["state"] == "off",
                  "active false becomes state off, active true does not")
            check(found["webhook:h2"]["name"] == "be\\nta",
                  "a newline in a webhook name is escaped  <-- pinned defect")
            check(seen[0]["auth"] == "Bearer " + secret and seen[0]["query"]
                  == "f=json" and secret not in seen[0]["query"],
                  "the token goes in X-Esri-Authorization, not the URL")
            check(seen[0]["other"] is None, "no Authorization header is sent")
            check(found["webhook:h1"]["info"] == "", "no failure text without the flag")
            full = probe("/ok/webhooks/", with_failures=True)
            check(full["webhook:h1"]["info"] ==
                  "5 logged failure(s), latest 2025-10-10T12:40:00Z",
                  "the probe counts the logged entries and names the latest")
            check(full["webhook:h2"]["info"] == "0 logged failure(s)",
                  "a webhook with no logged failures says so")
            check(stamp_from_millis(1760000000000) == "2025-10-09T08:53:20Z",
                  "epoch milliseconds convert to a UTC stamp")
            check(stamp_from_millis(10 ** 30) is None, "an absurd timestamp is unreadable")
            check(stamp_from_millis(True) is None and stamp_from_millis("x") is None,
                  "a boolean or text timestamp is unreadable")
            raises(lambda: probe("/err/webhooks"),
                   "an error object in a 200 body is REFUSED  <-- pinned defect",
                   "code 498")
            raises(lambda: probe("/err2/webhooks"), "a non-object error is refused",
                   "returned an error")
            raises(lambda: probe("/list/webhooks"), "a JSON list is refused",
                   "not a JSON object")
            raises(lambda: probe("/notjson/webhooks"), "an HTML body is refused",
                   "not JSON")
            raises(lambda: probe("/http500/webhooks"), "an HTTP 500 is refused",
                   "request failed")
            raises(lambda: probe("/missing/webhooks"), "an HTTP 404 is refused",
                   "request failed")
            raises(lambda: probe("/junk/webhooks"), "a malformed reply is refused",
                   "request failed")
            raises(lambda: probe_webhooks(base + "/big/webhooks", "", opener,
                                          max_bytes=100),
                   "an oversize body is refused", "larger than 100")
            before = len(seen)
            raises(lambda: probe("/redir/webhooks"),
                   "a redirect is REFUSED and not followed  <-- pinned defect",
                   "request failed")
            check(len(seen) == before + 1 and seen[-1]["path"] == "/redir/webhooks"
                  and not [r for r in seen if r["path"] == "/landed"],
                  "the redirect target was never contacted")
            raises(lambda: probe("/none/webhooks"), "no webhooks list is refused",
                   "no webhooks list")
            raises(lambda: probe("/noactive/webhooks"),
                   "a webhook with no active value is refused, not assumed on"
                   "  <-- pinned defect", "active")
            raises(lambda: probe("/nodict/webhooks"), "a non-object webhook is refused")
            raises(lambda: probe("/badid/webhooks"), "an unusable webhook id is refused")
            raises(lambda: probe("/nostat/webhooks", with_failures=True),
                   "a notification status with no list is refused", "no list")
            raises(lambda: probe("/ok"), "a URL not ending in /webhooks is refused",
                   "/webhooks")
            check(probe("/empty/webhooks") == {}, "a service with no webhooks is empty")
            raises(lambda: probe_webhooks("http://127.0.0.1:1/x/webhooks", "",
                                          opener),
                   "a refused connection is an error", "request failed")

            # the report path with a probe
            marker = wr("hooks.jsonl", (
                record_line("webhook:h1", NOW - datetime.timedelta(days=1), True)
                + "\n" + record_line("webhook:h2", NOW, True) + "\n"
                ).encode("ascii"))
            os.environ[TOKEN_ENV] = "  " + secret + "\n"
            code, out, err = run(["--ledger", marker, "--webhooks-url",
                                  base + "/ok/webhooks", "--with-failures"]
                                 + now_arg, opener)
            check(code == 1, "an OFF webhook exits 1")
            rows = dict((l.split()[0], l.split()[1]) for l in out.splitlines()[2:-1])
            check(rows == {"webhook:h1": "OK", "webhook:h2": "OFF"},
                  "the report joins the probe state and the ledger")
            check("(alpha) 5 logged failure(s)" in out, "the report shows the name and failures")
            check(secret not in out + err,
                  "the token is not in the report  <-- pinned defect")
            os.environ[TOKEN_ENV] = "bad token"
            code, out, err = run(["--ledger", marker, "--webhooks-url",
                                  base + "/ok/webhooks"] + now_arg, opener)
            check(code == 2 and "bad token" not in err and TOKEN_ENV in err,
                  "a malformed token exits 2 without printing it")
            os.environ.pop(TOKEN_ENV)
            n = len(seen)
            code, out, err = run(["--ledger", marker, "--webhooks-url",
                                  base + "/ok/webhooks"] + now_arg, opener)
            check(code == 1 and seen[n]["auth"] is None,
                  "with no token set no auth header is sent")
            code, out, err = run(["--ledger", marker, "--webhooks-url",
                                  base + "/err/webhooks"] + now_arg, opener)
            check(code == 2 and "code 498" in err and out == "",
                  "a failed probe exits 2 and prints no verdict")
            code, out, err = run(["--ledger", marker, "--webhooks-url",
                                  "http://example.com/webhooks"] + now_arg, opener)
            check(code == 2 and "https" in err,
                  "plain http to another host is refused")
        finally:
            server.shutdown()
            server.server_close()
            thread.join()
            os.environ[TOKEN_ENV] = env or ""  # empty is unset to this tool

        # ---- URL and token rules
        check(check_url("https://gis.example.com/a/webhooks") is None
              and check_url("http://localhost:6080/x/webhooks") is None,
              "https and loopback http pass the URL check")
        for bad in ("https://h/webhooks?token=abc", "https://u:p@h/webhooks",
                    "https://h/webhooks#x", "ftp://h/webhooks", "https:///x",
                    "http://gis.example.com/webhooks"):
            raises(lambda bad=bad: check_url(bad), "URL refused: %s" % bad[:30])
        check(check_token(" abc123 \r\n") == "abc123", "a token is trimmed")
        raises(lambda: check_token("a b"), "a token with a space is refused")
        raises(lambda: check_token("a\x01b"), "a token with a control character is refused")
        raises(lambda: check_token(u"caf\u00e9"), "a non-ASCII token is refused")

        # ---- nothing leaks
        def holds_token(name):
            with open(os.path.join(work, name), "rb") as fh:
                return secret.encode("ascii") in fh.read()

        check(not [n for n in os.listdir(work) if holds_token(n)],
              "no file the run wrote holds the token")
        src = os.path.abspath(__file__)
        with open(src, "rb") as fh:
            code_bytes = fh.read()
        check(all(b < 128 for b in bytearray(code_bytes)),
              "stalled.py is pure ASCII")
    finally:
        for p in made:
            if os.path.isfile(p):
                os.remove(p)
        os.rmdir(work)

    # ---- the harness itself: a failed check, a missing raise, a wrong
    # exception and the failing footer must all be counted, not swallowed
    probe_h = Harness()
    sink = io.StringIO()
    with contextlib.redirect_stdout(sink):
        probe_h.check(True, "p1")
        probe_h.check(False, "p2")
        probe_h.raises(lambda: None, "p3")
        probe_h.raises(lambda: 1 / 0, "p4")
        probe_h.raises(lambda: check_name("a b"), "p5", "no such text")
        probe_h.raises(lambda: check_name("a b"), "p6", "job name")
        code = probe_h.finish()
    check(probe_h.passed == 2 and probe_h.failed[0] == "p2"
          and len(probe_h.failed) == 4 and code == 1,
          "the harness counts a failed check, a missing raise and a "
          "wrong exception")
    check("4 failed" in sink.getvalue() and "FAILED: p3" in sink.getvalue()
          and "wrong exception" in sink.getvalue(),
          "the harness footer lists each failure")

    return harness.finish()


if __name__ == "__main__":  # pragma: no branch
    sys.exit(main())
