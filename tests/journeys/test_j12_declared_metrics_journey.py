"""Journey J12: what my agent declares it measures is what Trinity knows (ent#477).

**Promise:** I write a `metrics:` block in my agent's `template.yaml`, and from
that point on Trinity knows those metrics — at creation, after a pull, after a
restart, and after I edit the block inside the container and ask for a refresh.
I never have to tell Trinity twice, and I am never silently ignored.

The epic (ent#476) asked for the journey id "J11-declared-metrics"; J11 was
already taken by #2565's brief-delivery promise, so this ships as **J12**.

**A skeleton, deliberately.** ent#477 builds the registry; the *values* half —
`record_metrics` (ent#478) and the read/freshness surface (ent#479) — is what
turns "Trinity knows the declaration" into something a person sees. The
`strict=True` xfail is what Gate G1 asks a `new:` declaration to carry: a
named, failing-on-success claim standing in the tier. `strict` is the
load-bearing half — if this ever passes by accident the suite goes RED, so the
skeleton cannot quietly become a test that asserts nothing.

Live-stack by necessity, not by preference: every trigger except creation reads
`template.yaml` out of a RUNNING container over the Docker socket, which is
exactly the leg the ent#477 unit suite declares as its own gap.
"""
import pytest

pytestmark = pytest.mark.journey


@pytest.mark.xfail(
    strict=True,
    reason="J12 harness not built — abilityai/trinity-enterprise#477. The "
           "registry ships here; the values half it exists to serve is "
           "ent#478/ent#479, and the end-to-end promise needs a real agent "
           "container, a real git pull and a real restart.",
)
def test_j12_a_declared_metric_is_what_trinity_knows():
    """The promise, not the plumbing.

    What this has to assert when it is built, in the user's own terms:

    1. create an agent from a template whose `metrics:` block declares a
       counter, a status metric and a cadence — `GET /api/agents/{name}/
       metrics/definitions` returns all three with their labels, thresholds,
       status values and `cadence_seconds`, read back through the API a person
       would use;
    2. edit `template.yaml` INSIDE the container (the way an agent edits its
       own), then `POST .../metrics/definitions/refresh` — the change is
       there, without a restart and without a `git pull`;
    3. restart the container without asking for anything — the change is there
       anyway (the T1 start hook, which is the path an agent that pushes its
       own edits actually takes);
    4. remove a metric from the block and refresh — it is RETIRED, still
       readable with `?include_retired=true`, and re-declaring the same name
       brings the same row back rather than a second one;
    5. change a metric's `type` — Trinity refuses, keeps the stored type, and
       SAYS SO in `type_conflict` and the refresh summary. The user is never
       silently overruled;
    6. stop the agent and refresh — a named 409, and the registry still holds
       everything it held before. Break the YAML and refresh — a named 503,
       and again nothing is lost. "Trinity could not read it" never looks like
       "you deleted it".

    The live-stack halves this needs and the unit suite cannot give it: a real
    Docker exec against a real container, a real `git pull` that adopts a new
    template, and a real container restart.
    """
    raise AssertionError("J12 harness not built")
