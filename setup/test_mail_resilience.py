"""Test: the morning questions survive a mail outage (home page plan, 2026-11-12, step 1).

Uses a throwaway app folder (reports, queue) and a fake send_daily; never touches the real reports or mail.
  - a failed send (502 / exception) is recorded on the proposal as not delivered
  - a second prep on the same day keeps the open proposal's link (web_token)
  - catch_up_send re-sends an undelivered update once mail works, at most 3 tries
  - mail_health counts failures in a row and clears them on success
Run: python setup\\test_mail_resilience.py
"""
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "jobs"))
import common as C  # noqa: E402
import organize as O  # noqa: E402

fails = 0


def check(cond, what):
    global fails
    print(("ok   " if cond else "FAIL ") + what)
    fails += 0 if cond else 1


with tempfile.TemporaryDirectory() as tmp:
    tmp = Path(tmp)
    (tmp / "reports").mkdir()
    (tmp / "queue").mkdir()
    (tmp / "local" / "work").mkdir(parents=True)
    C.APP = tmp
    cfg = {"local_root": str(tmp / "local"), "timezone": "America/Chicago"}
    log = print
    day = C.now(cfg).strftime("%Y-%m-%d")
    f = tmp / "reports" / f"organize-{day}.json"
    sent = []

    def fake(code):
        def send_daily(cfg_, log_, day_, prop_, dry_=False):
            sent.append(day_)
            if isinstance(code, Exception):
                raise code
            return code
        return send_daily

    # 1. a 502 is recorded as not delivered
    prop = {"day": day, "status": "sent", "web_token": "first", "questions": []}
    f.write_text(json.dumps(prop), encoding="utf-8")
    O.U.send_daily = fake(502)
    O.send_and_record(cfg, log, f, day, prop, False)
    rec = json.loads(f.read_text(encoding="utf-8"))["email"]
    check(rec["sent"] is False and rec["code"] == 502 and rec["tries"] == 1, "502 recorded as not delivered")

    # 2. an exception does not escape and is recorded
    O.U.send_daily = fake(ConnectionError("graph down"))
    O.send_and_record(cfg, log, f, day, json.loads(f.read_text(encoding="utf-8")), False)
    rec = json.loads(f.read_text(encoding="utf-8"))["email"]
    check(rec["sent"] is False and "graph down" in rec["error"] and rec["tries"] == 2, "exception recorded, not raised")

    # 3. a second prep keeps the open proposal's token and email record; the old copy is kept outside reports
    new = {"day": day, "status": "sent", "web_token": "second", "questions": [{"id": 1}]}
    O.keep_open_token(cfg, log, f, new, False)
    check(new["web_token"] == "first", "second prep keeps the emailed link's token")
    check(new.get("email", {}).get("tries") == 2, "second prep keeps the email record")
    check(len(list((tmp / "local" / "work" / "organize-replaced").glob("*.json"))) == 1, "replaced proposal saved in work")
    check(len(list((tmp / "reports").glob("organize-*.json"))) == 1, "no extra open day in reports")

    # 4. an applied proposal is not touched by keep_open_token
    f2 = tmp / "reports" / "organize-2000-01-01.json"
    f2.write_text(json.dumps({"day": "2000-01-01", "status": "applied", "web_token": "old"}), encoding="utf-8")
    other = {"web_token": "fresh"}
    O.keep_open_token(cfg, log, f2, other, False)
    check(other["web_token"] == "fresh", "applied proposal: new token used")
    f2.unlink()

    # 5. catch-up: one more try (3rd), then never a 4th
    sent.clear()
    O.U.send_daily = fake(202)
    O.catch_up_send(cfg, log)
    rec = json.loads(f.read_text(encoding="utf-8"))["email"]
    check(sent == [day] and rec["sent"] is True and rec["tries"] == 3, "catch-up re-sends once mail works")
    sent.clear()
    O.catch_up_send(cfg, log)
    check(sent == [], "delivered: no further catch-up")
    d = json.loads(f.read_text(encoding="utf-8"))
    d["email"] = {"sent": False, "tries": 3}
    f.write_text(json.dumps(d), encoding="utf-8")
    O.catch_up_send(cfg, log)
    check(sent == [], "3 tries: no 4th")

    # 5b. a new morning's questions close off earlier open days (asked again), never later or answered ones
    for d, st in (("2000-01-01", "sent"), ("2000-01-02", "applied")):
        (tmp / "reports" / f"organize-{d}.json").write_text(json.dumps({"day": d, "status": st, "web_token": d}),
                                                            encoding="utf-8")
    O.supersede_older(cfg, log, day)
    old = json.loads((tmp / "reports" / "organize-2000-01-01.json").read_text(encoding="utf-8"))
    done = json.loads((tmp / "reports" / "organize-2000-01-02.json").read_text(encoding="utf-8"))
    check(old["status"] == "superseded" and old["superseded_by"] == day, "earlier open day superseded")
    check(done["status"] == "applied", "answered day left alone")
    check(json.loads(f.read_text(encoding="utf-8"))["status"] == "sent", "today's questions stay open")
    check(list(O.pending()) == [day], "only today's questions are pending")
    for d in ("2000-01-01", "2000-01-02"):
        (tmp / "reports" / f"organize-{d}.json").unlink()

    # 6. mail health
    C.mail_health(cfg, "read", False, "HTTP 502")
    C.mail_health(cfg, "read", False, "HTTP 504")
    h = C.mail_health_read()["read"]
    check(h["failed_in_row"] == 2 and h["last_error"] == "HTTP 504" and "down_since" in h, "failures counted")
    C.mail_health(cfg, "read", True)
    h = C.mail_health_read()["read"]
    check(h["failed_in_row"] == 0 and "down_since" not in h and "last_ok" in h, "success clears the count")

print("\nall passed" if not fails else f"\n{fails} FAILED")
sys.exit(1 if fails else 0)
