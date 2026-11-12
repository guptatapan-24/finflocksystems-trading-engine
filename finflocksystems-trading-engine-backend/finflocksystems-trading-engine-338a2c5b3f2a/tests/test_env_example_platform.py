"""The hosting platform reads .env.example (baked into the image as .env) before every deploy and treats each BLANK
`KEY=` line as a secret it must attach; one it cannot attach fails the whole deploy (2026-11-03: six blank e-mail/alert
lines did exactly that). Only the keys the platform itself provides may be blank."""
import re
from pathlib import Path

PLATFORM_PROVIDED = {"REDIS_PASSWORD", "REDIS_USERNAME", "QCH_APP_ID", "QCH_API_HOST", "QCH_SERVICE_TOKEN"}


def test_env_example_has_no_blank_keys_the_platform_cannot_attach():
    text = (Path(__file__).resolve().parent.parent / ".env.example").read_text(encoding="utf-8")
    blank = {m.group(1) for m in re.finditer(r"^([A-Za-z_][A-Za-z0-9_]*)=\s*$", text, re.M)}
    assert blank - PLATFORM_PROVIDED == set(), "comment these out or give them a value"


def test_no_email_settings_while_email_is_on_hold():
    text = (Path(__file__).resolve().parent.parent / ".env.example").read_text(encoding="utf-8")
    assert not re.search(r"^(EMAIL_[A-Z_]+|DEFAULT_FROM_EMAIL|NFO_FRESHNESS_ALERT_TO)=", text, re.M)
