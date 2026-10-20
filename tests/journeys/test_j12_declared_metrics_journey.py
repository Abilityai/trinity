"""Journey J12: what my agent declares it measures is what Trinity knows (ent#477–#479).

**Promise:** I write a `metrics:` block in my agent's `template.yaml`, and from
that point on Trinity knows those metrics — at creation, after a pull, after a
restart, and after I edit the block inside the container and ask for a refresh.
I never have to tell Trinity twice, and I am never silently ignored.

The epic (ent#476) asked for the journey id "J11-declared-metrics"; J11 was
already taken by #2565's brief-delivery promise, so this ships as **J12**.

**A skeleton, deliberately.** The promise is now WHOLE in code — ent#477 builds
the registry, ent#478 records points against it, ent#479 reads them back with a
freshness verdict and renders them — so what is missing here is the walk, not
the feature. Each leg has unit coverage (`test_ent477_*`, `test_ent478_*`,
`test_ent479_*`); none of that is the same as doing the loop once against a
real container, which is the only way to catch a seam between the three. The
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
    reason="J12 harness not built — abilityai/trinity-enterprise#477/#478/#479. "
           "All three legs ship; the end-to-end walk needs a real agent "
           "container, a real git pull and a real restart, which the unit "
           "island by construction cannot provide.",
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
       "you deleted it";
    7. **record and read back** (ent#478 + ent#479, the half that makes the
       promise visible): call `record_metrics` for the counter and the status
       metric, then `GET /api/agents/{name}/metrics` and find both — with the
       value, the point time, and `freshness: "fresh"` decided by the declared
       cadence. Advance past `2 × cadence` and the SAME read says
       `stale: true` with a `stale_after`, while the value it last knew is
       still there. Declare a metric with no `cadence:` and it is never stale
       (`stale: null`), because nothing said what late would mean;
    8. **stop the agent and read again** — identical answer. The read is
       store-backed and contacts no container, which is the whole reason a
       number survives the thing that produced it;
    9. write a `metrics.json` into the workspace, run the compatibility
       collector, and find **D-010** naming it superseded — and the read
       echoing that finding — rather than the file's numbers being served as
       current.

    The live-stack halves this needs and the unit suite cannot give it: a real
    Docker exec against a real container, a real `git pull` that adopts a new
    template, a real container restart, and a real `record_metrics` call from
    inside an agent through the MCP server.
    """
    raise AssertionError("J12 harness not built")
