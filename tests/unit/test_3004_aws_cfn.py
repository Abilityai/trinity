"""The CloudFormation Launch Stack template (#3004, PROV-019).

Parsed as plain YAML (the template uses long-form intrinsics for exactly this),
then held to the security posture the issue sets: 80/443 open, 22 closed unless
the operator names a source range, no IAM role, an Elastic IP, and the instance
ID surfaced because /setup asks for it.
"""
from __future__ import annotations

from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

_TEMPLATE = Path(__file__).resolve().parents[2] / "packer" / "aws" / "trinity.cfn.yaml"


@pytest.fixture(scope="module")
def cfn() -> dict:
    return yaml.safe_load(_TEMPLATE.read_text())


def _of_type(cfn: dict, t: str) -> dict:
    return {k: v for k, v in cfn["Resources"].items() if v["Type"] == t}


def test_parameters(cfn):
    p = cfn["Parameters"]
    assert p["ImageId"]["Type"] == "AWS::EC2::Image::Id"
    assert p["InstanceType"]["Default"] == "t3a.large"
    assert p["KeyName"]["Default"] == "" and p["SshCidr"]["Default"] == ""
    # Quick-create links cannot prefill a NoEcho value, and nothing here is
    # secret: no password parameter may exist at all.
    assert not any("assword" in name for name in p)


def test_only_https_and_http_are_open_to_the_internet(cfn):
    (sg,) = _of_type(cfn, "AWS::EC2::SecurityGroup").values()
    ports = {(r["FromPort"], r["ToPort"], r["CidrIp"]) for r in sg["Properties"]["SecurityGroupIngress"]}
    assert ports == {(443, 443, "0.0.0.0/0"), (80, 80, "0.0.0.0/0")}


def test_ssh_is_opened_only_to_a_range_the_operator_names(cfn):
    rules = _of_type(cfn, "AWS::EC2::SecurityGroupIngress")
    assert len(rules) == 1
    (rule,) = rules.values()
    assert rule["Condition"] == "HasSshCidr"
    assert rule["Properties"]["FromPort"] == 22
    assert rule["Properties"]["CidrIp"] == {"Ref": "SshCidr"}


def test_instance_eip_and_no_iam(cfn):
    (inst,) = _of_type(cfn, "AWS::EC2::Instance").values()
    assert inst["Properties"]["ImageId"] == {"Ref": "ImageId"}
    assert "IamInstanceProfile" not in inst["Properties"]
    assert not [r for r in cfn["Resources"].values() if r["Type"].startswith("AWS::IAM::")]
    (eip,) = _of_type(cfn, "AWS::EC2::EIP").values()
    assert eip["Properties"]["InstanceId"] == {"Ref": "Instance"}


def test_outputs_carry_the_url_and_the_claim_value(cfn):
    out = cfn["Outputs"]
    assert out["InstanceId"]["Value"] == {"Ref": "Instance"}
    assert "https://" in str(out["TrinityUrl"]["Value"])
