#!/usr/bin/env python3
"""Print a fresh one-time magic sign-in link (expires in 15 minutes), made exactly the way the app
makes them (web.app.issue_magic_link: nonce in data/agenda_watch.db, record in outbox/magic/).

    .venv/bin/python scripts/make_signin_link.py test+portal@example.invalid

Sends NO email: it refuses the owner address (the only one the app could ever email), and for
every other address the app only stores the link. Also written to SIGNIN_LINK.txt.
Each link works once; make a new one right before you use it.
"""
import io
import contextlib
import logging
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def main():
    if len(sys.argv) != 2:
        raise SystemExit("usage: make_signin_link.py EMAIL")
    env = ROOT / ".env"
    if env.is_file():
        for line in env.read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())
    logging.disable(logging.INFO)
    from web import app as webapp
    email = sys.argv[1].strip().lower()
    if not webapp.EMAIL_RE.match(email):
        raise SystemExit(f"not a valid email: {email}")
    if email == webapp.OWNER:
        raise SystemExit("Refusing the owner address: the app would email it. Use the site's sign-in form.")
    with contextlib.redirect_stdout(io.StringIO()):
        link, status = webapp.issue_magic_link(email)
    assert "no email sent" in status, status
    (ROOT / "SIGNIN_LINK.txt").write_text(
        f"Agenda Watch magic link for {email} (one-time, expires in {webapp.LINK_TTL // 60} min)\n{link}\n")
    print(link)


if __name__ == "__main__":
    main()
