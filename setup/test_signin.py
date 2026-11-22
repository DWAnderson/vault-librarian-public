"""Simulates an expired Claude sign-in (bogus CLAUDE_CODE_OAUTH_TOKEN) against claude_cli, llm.run and
claude_auth, with state in a temp file and mail captured instead of sent. 2026-10-24."""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "jobs"))
import common as C  # noqa: E402
import claude_auth as A  # noqa: E402
import claude_cli  # noqa: E402
import llm  # noqa: E402

tmp = Path(tempfile.mkdtemp(prefix="signin-test-", dir=str(C.APP / "setup")))
A.STATE = tmp / "claude-signin.json"
sent = []
C.send_mail = lambda cfg, subj, html, to=None: sent.append((subj, html)) or 202
bogus = dict(os.environ, CLAUDE_CODE_OAUTH_TOKEN="sk-ant-oat01-bogus-for-test")
A.env = lambda cfg=None: bogus
llm._log_call = lambda *a, **k: None
cfg = C.load_config()

out, info = claude_cli.ask("Reply with OK.", "", tmp, timeout=90, model="haiku")
print("1 ask ->", out, info)
assert out is None and info.get("auth"), "auth error not recognised"
print("  state:", A.load())
print("  mails:", [s for s, _ in sent])

ans = llm.run(cfg, "email", [{"id": "a", "instructions": "x", "material": "y"},
                             {"id": "b", "instructions": "x", "material": "y"}], tmp / "answers", log=print, item="t")
print("2 llm.run ->", ans)
assert ans["a"]["limit"] and ans["b"].get("skipped"), "queue items would not wait"

A.record_failure("Failed to authenticate: test", "the 06:00 filing proposal", cfg, missed="organize")
print("3 after organize failure: mails", len(sent), "(should still be 1 - no repeat within 12 h)")
assert len(sent) == 1

A.env = lambda cfg=None: dict(os.environ)    # the real sign-in again
r = A.recover.__wrapped__ if hasattr(A.recover, "__wrapped__") else None
started = []
import types  # noqa: E402
A.subprocess = types.SimpleNamespace(Popen=lambda *a, **k: started.append(a[0]))
A.recover(cfg, log=print)
print("4 recover -> state", A.load(), "catch-up started:", [Path(x[1]).name for x in started])
assert A.load()["ok"] and started, "recovery/catch-up didn't happen"
print("\nEMAIL TEXT:\n", sent[0][0], "\n", sent[0][1])
print("\nALL PASSED")
