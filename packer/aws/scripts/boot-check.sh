#!/bin/bash
# Boot a throwaway stack from an AMI and test it the way a customer uses it.
# AWS counterpart of packer/digitalocean/scripts/boot-check.sh.
#
#   AWS_PROFILE=<publishing account> bash boot-check.sh <ami-id> <tag>
#
# A pass writes ~/.trinity-marketplace/<ami-id>.pass, which mp-submit.sh requires.
# Any failure, and any check that cannot run, exits non-zero. The stack and the
# temporary key pair are deleted on every exit a shell can trap. If they cannot be
# (a closed laptop), the instance stops itself after 45 minutes; a stopped instance
# still bills for its volume and Elastic IP, so delete the stack by hand.
# Every run first removes the AMI's earlier pass, so the newest result decides.
# Chat with an agent is not tested, because it needs a model credential.
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
. "$HERE/../../digitalocean/scripts/boot-lib.sh"
export AWS_DEFAULT_REGION=us-east-1

TIMEOUT="${TRINITY_BOOT_CHECK_TIMEOUT:-1200}"
POLL="${TRINITY_BOOT_CHECK_POLL:-5}"
AGENT="boot-check-agent"
TEMPLATE="$HERE/../trinity.cfn.yaml"

say()  { echo "[boot-check] $*"; }
fail() { echo "[boot-check] FAIL: $*" >&2; exit 1; }

AMI="${1:-}"
TAG="${2:-}"
if ! [[ "$AMI" =~ ^ami-[0-9a-f]+$ && "$TAG" =~ ^v[0-9]+\.[0-9]+\.[0-9]+(-[A-Za-z0-9.-]+)?$ ]]; then
    echo "usage: boot-check.sh <ami-id> <tag>" >&2
    exit 2
fi
[ -f "$TEMPLATE" ] || fail "template not found: ${TEMPLATE}"

RECORD="${HOME}/.trinity-marketplace/${AMI}.pass"
rm -f "$RECORD"

read -r state imds tagged <<<"$(aws ec2 describe-images --image-ids "$AMI" --output text \
    --query 'Images[0].[State,ImdsSupport,Tags[?Key==`trinity_image_tag`]|[0].Value]')"
[ "$state" = "available" ] || fail "AMI ${AMI} is ${state:-not found}"
[ "$tagged" = "$TAG" ] || fail "AMI ${AMI} was built for ${tagged}, which is another tag than ${TAG}"
[ "$imds" = "v2.0" ] || fail "AMI ${AMI} does not require IMDSv2"

WORK="$(mktemp -d)"
RUN="trinity-boot-check-$(date +%s)-$$"
IP=""
STACK_CREATED=""
KEY_CREATED=""
SSH_OPTS=(-i "${WORK}/key" -o BatchMode=yes -o IdentitiesOnly=yes -o ConnectTimeout=10
          -o ServerAliveInterval=15 -o ServerAliveCountMax=3
          -o StrictHostKeyChecking=accept-new -o "UserKnownHostsFile=${WORK}/known_hosts")

# The AMI logs in as ubuntu; everything checked on the host needs root.
rsh() { ssh -n "${SSH_OPTS[@]}" "ubuntu@${IP}" "sudo bash -c $(printf '%q' "$1")"; }

cleanup() {
    local rc=$?
    # Signals are ignored from here: nothing else deletes a billing stack.
    trap - EXIT
    trap '' INT TERM HUP
    if [ "$rc" != 0 ] && [ -n "$IP" ]; then
        say "first boot log and containers:"
        rsh 'tail -n 40 /var/log/trinity-firstboot.log; timeout 30 docker ps -a --format "{{.Names}} {{.Status}}"' 2>&1 | sed 's/^/  /'
    fi
    if [ -n "$STACK_CREATED" ]; then
        say "deleting stack ${RUN}"
        aws cloudformation delete-stack --stack-name "$RUN"
        if aws cloudformation wait stack-delete-complete --stack-name "$RUN"; then
            say "stack deleted"
        else
            echo "[boot-check] FAIL: stack ${RUN} was not deleted. It is still billing." >&2
            rc=1
        fi
    fi
    if [ -n "$KEY_CREATED" ]; then
        aws ec2 delete-key-pair --key-name "$RUN" >/dev/null || { echo "[boot-check] FAIL: key pair ${RUN} left behind" >&2; rc=1; }
    fi
    rm -rf "$WORK"
    exit "$rc"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
trap 'exit 129' HUP

aws ec2 create-key-pair --key-name "$RUN" --query KeyMaterial --output text > "${WORK}/key" \
    || fail "key pair create failed"
KEY_CREATED=1
chmod 600 "${WORK}/key"

MY_IP="$(curl -fsS --max-time 10 https://checkip.amazonaws.com | tr -d '[:space:]')"
[[ "$MY_IP" =~ ^[0-9.]+$ ]] || fail "could not read this machine's public address"

say "creating stack ${RUN} from ${AMI}"
aws cloudformation create-stack --stack-name "$RUN" \
    --template-body "file://${TEMPLATE}" \
    --tags "Key=purpose,Value=trinity-boot-check" \
    --parameters "ParameterKey=ImageId,ParameterValue=${AMI}" \
                 "ParameterKey=KeyName,ParameterValue=${RUN}" \
                 "ParameterKey=SshCidr,ParameterValue=${MY_IP}/32" >/dev/null \
    || fail "stack create failed"
STACK_CREATED=1
aws cloudformation wait stack-create-complete --stack-name "$RUN" || fail "stack did not reach CREATE_COMPLETE"

output() {
    aws cloudformation describe-stacks --stack-name "$RUN" --output text \
        --query "Stacks[0].Outputs[?OutputKey=='$1'].OutputValue"
}
INSTANCE_ID="$(output InstanceId)"
BASE="$(output TrinityUrl)"; BASE="${BASE%/}"
IP="${BASE#https://}"
[[ "$INSTANCE_ID" =~ ^i- && "$IP" =~ ^[0-9.]+$ ]] || fail "could not read the stack outputs"
say "instance ${INSTANCE_ID} at ${IP}"

DEADLINE=$((SECONDS + TIMEOUT))

# ponytail: the template takes no user data, so the shutdown is armed over SSH and an
# instance that never answers SSH is never armed. A UserData parameter on the template
# is the upgrade.
ssh_up() { rsh true >/dev/null 2>&1; }
wait_for "SSH" ssh_up
rsh 'shutdown -P +45' >/dev/null 2>&1 || fail "could not arm the 45-minute shutdown"

wait_for "the backend" backend_up

# The security group is the only thing closing the host's ports on AWS, and its
# rules are the record, so read them. This also holds on a network that answers TCP
# itself, where probing from this machine proves nothing.
ingress="$(aws ec2 describe-security-groups --output text \
    --filters "Name=tag:aws:cloudformation:stack-name,Values=${RUN}" \
    --query 'SecurityGroups[0].IpPermissions[].[FromPort,ToPort,IpProtocol,IpRanges[0].CidrIp]' | sort -n | tr '\t' ' ')"
expected="$(printf '22 22 tcp %s/32\n80 80 tcp 0.0.0.0/0\n443 443 tcp 0.0.0.0/0' "$MY_IP")"
[ "$ingress" = "$expected" ] || fail "unexpected security group rules: ${ingress}"
say "security group admits 80, 443 and SSH from this machine only"

# /setup demands the instance ID, and refuses a wrong one.
status="$(rsh 'curl -fsS -m 5 http://localhost:8000/api/setup/status')" || fail "setup status unreadable"
[ "$(jq -r .setup_completed <<<"$status")" = "false" ] || fail "the AMI already has an admin"
[ "$(jq -r .claim_required <<<"$status")" = "instance-id" ] || fail "/setup does not require the instance ID"

PASSWORD="Aa1-$(openssl rand -hex 16)"
claim() {
    # email is a required field; a blank one is what the AWS path accepts.
    printf '{"password":"%s","confirm_password":"%s","email":"","claim_code":"%s"}' "$PASSWORD" "$PASSWORD" "$1" \
        | ssh "${SSH_OPTS[@]}" "ubuntu@${IP}" "curl -sS -m 30 -o /dev/null -w '%{http_code}' -H 'Content-Type: application/json' --data-binary @- http://localhost:8000/api/setup/admin-password"
}
code="$(claim i-00000000000000000)"
[ "$code" = "403" ] || fail "a wrong instance ID returned ${code}, expected 403"
code="$(claim "$INSTANCE_ID")"
[ "$code" = "200" ] || fail "claiming /setup with the instance ID returned ${code}"
say "/setup refused a wrong instance ID and accepted the right one"

wait_for_first_boot

# The Elastic IP can attach after first boot read the temporary address. The refresh
# timer then moves the certificate, within 5 minutes by design, so this waits.
page_ok() { curl -fsS -i --max-time 15 "${BASE}/" > "${WORK}/page" 2>/dev/null; }
wait_for "a trusted certificate on ${IP}" page_ok
grep -qiE '<title>[^<]*trinity' "${WORK}/page" || fail "${BASE}/ does not serve Trinity"
if grep -qiE '^x-do-marketplace:' "${WORK}/page"; then fail "${BASE}/ sends the DigitalOcean marketplace header"; fi
say "HTTPS serves Trinity with a trusted certificate"
say "first boot tls-status: $(rsh 'cat /etc/trinity/tls-status')"

[ "$(rsh 'cat /etc/trinity/cloud')" = "aws" ] || fail "/etc/trinity/cloud is another cloud than aws"
check_install aws-marketplace
sign_in_and_create_agent
check_services

rsh 'iptables -C DOCKER-USER -j TRINITY-FW' || fail "the DOCKER-USER firewall rule is missing"
# The host reaches metadata and a container must not, or the check proves nothing.
rsh 'T=$(curl -fsS -m 5 -X PUT -H "X-aws-ec2-metadata-token-ttl-seconds: 60" http://169.254.169.254/latest/api/token) && [ -n "$T" ]' \
    || fail "the host cannot reach metadata, so the container check proves nothing"
# Only a curl timeout or refusal counts as blocked: a missing curl or a stopped
# container exits non-zero too, and must not read as a pass.
rsh "timeout 20 docker exec agent-${AGENT} curl -sS -m 5 -o /dev/null http://169.254.169.254/latest/meta-data/" 2>/dev/null
rc=$?
[ "$rc" = 28 ] || [ "$rc" = 7 ] || fail "the container metadata check exited ${rc}, expected a curl timeout (28) or refusal (7)"
say "firewall rule present, containers cannot reach metadata"

# ponytail: local file, so a submit must run on the machine that tested, as on DigitalOcean.
mkdir -p -m 700 "${HOME}/.trinity-marketplace"
printf 'tag=%s\n' "$TAG" > "$RECORD"
say "PASS: ${AMI} (${TAG})"
