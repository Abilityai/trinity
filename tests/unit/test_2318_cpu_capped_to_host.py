"""
#2318: containers_run caps the requested CPU limit at the Docker host's CPU
count. Docker rejects NanoCpus above the host's CPUs with a 400, so a template
asking for cpu "4" (the trinity-system template does) could never be created on
a 2-CPU host.

Module: src/backend/services/docker_utils.py
"""

import pytest

from test_docker_utils import get_docker_utils

GB = 1_000_000_000


async def _run_with(host_cpus, **kwargs):
    docker_utils, client = get_docker_utils()
    client.info.return_value = {"NCPU": host_cpus}
    await docker_utils.containers_run("img", detach=True, **kwargs)
    return client.containers.run.call_args.kwargs


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "requested,host,expected",
    [
        (4, 2, 2),   # the trinity-system template on a t3a.large
        (4, 4, 4),   # fits: unchanged
        (2, 8, 2),   # below the host: unchanged
        (8, 3, 3),   # an odd host count is a valid Docker limit
    ],
)
async def test_nano_cpus_capped_to_host(requested, host, expected):
    kwargs = await _run_with(host, nano_cpus=requested * GB)
    assert kwargs["nano_cpus"] == expected * GB


@pytest.mark.unit
@pytest.mark.asyncio
async def test_no_cpu_limit_is_left_alone():
    kwargs = await _run_with(2)
    assert "nano_cpus" not in kwargs


@pytest.mark.unit
@pytest.mark.asyncio
async def test_unreadable_host_count_passes_request_through():
    docker_utils, client = get_docker_utils()
    client.info.side_effect = RuntimeError("daemon unreachable")
    await docker_utils.containers_run("img", detach=True, nano_cpus=4 * GB)
    assert client.containers.run.call_args.kwargs["nano_cpus"] == 4 * GB
