"""Workspace person tags — the tagged person's door (trinity-enterprise#631).

A tag is a pointer, not a seat: one `operator_queue` row addressed to the person
who was named (the ledger behind the Inbox's four doors, ent#610). This package
is only the ROUTE the Inbox reads it through; the rules live in
`services/person_mention_service.py` (shared with rooms and the 1:1 chat) and
the row shapes in `db/queue_mentions.py`.

Under `client_portal/` for the reason `asks/` is: it is a Workspace surface on
the Workspace prefix, and it inherits `client_portal`'s Dockerfile COPY line
(the #1033 trap). OSS core, ungated, like the rest of the Inbox.
"""
