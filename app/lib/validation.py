"""Input rules shared by the browser and the server, so both accept exactly the same values.

EMAIL_PATTERN is used as the HTML `pattern` attribute on email fields (the browser anchors it) and, anchored,
by the login and invite routes. It needs `name@domain.tld`: at least one dot after the @, and a final part of
2+ letters. So `sam@acme.io` and `sam@mail.acme.co.uk` pass; `sam@acme`, `sam@acme.` and `sam@acme.c` don't.
(The browser's own type="email" check alone accepts `sam@acme`.)

Hyphens inside character classes are escaped because modern browsers compile `pattern` with the `v` regex flag.
"""

import re

EMAIL_PATTERN = r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9\-]+(\.[A-Za-z0-9\-]+)*\.[A-Za-z]{2,}"
EMAIL_HINT = "Enter a valid email address, like name@company.com."
_EMAIL_RE = re.compile(rf"^{EMAIL_PATTERN}$")


def is_valid_email(value: str | None) -> bool:
    return bool(_EMAIL_RE.match((value or "").strip()))
