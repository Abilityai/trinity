"""The terms of use a Trinity instance ships with (trinity-enterprise#1107).

Shown to every first-time person on every door until an administrator replaces
the text in Settings › Access. Plain, operator-neutral wording: the instance
operator is "the operator", the people signing in are "you". It is a default,
not legal advice — an operator with their own terms pastes them over it, and
``POST /api/terms/restore-default`` brings this text back in one action.

``DEFAULT_TERMS_VERSION`` is the version a person accepts while the default is
in force. It is a constant, not a timestamp, so restoring the default after a
custom text does not ask everyone who already accepted the default again (the
text they accepted is byte-identical). Changing this text in a release bumps
the constant, which asks again — the honest behaviour for a changed text.
"""
from __future__ import annotations

DEFAULT_TERMS_TITLE = "Terms of use"

DEFAULT_TERMS_VERSION = "default-1"

DEFAULT_TERMS_TEXT = """\
## Terms of use

These terms apply to your use of this Trinity instance. By accepting them you
agree to the following.

### What this is

This instance runs AI agents on behalf of its operator. The agents can read the
information you give them, act on your instructions, and use the tools and
accounts the operator has connected. Their answers are generated and may be
wrong; check anything that matters before you rely on it.

### Your account

- Keep your sign-in to yourself. You are responsible for what is done from it.
- Use the instance for the purposes the operator made it available for, and
  within the law.
- Do not try to reach data, agents or settings you were not given access to.

### Your data

- What you type, upload or connect may be stored on this instance and sent to
  the AI providers the operator has configured, so the agents can do their
  work.
- The operator can read activity on this instance, including conversations
  with agents, to keep it running and secure.
- Do not share credentials or secrets in a conversation; use the secret field
  an agent offers, which stores the value without showing it.

### Availability and changes

The operator may change, suspend or remove agents, features or accounts at any
time, and may update these terms. When the terms change you will be asked to
accept the new version before you continue.

### No warranty

The instance and its agents are provided as they are. To the extent the law
allows, the operator is not liable for loss or damage arising from their use.

If you do not agree, do not continue: sign out, and ask the operator if you
have questions.
"""
