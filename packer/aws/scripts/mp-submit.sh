#!/bin/bash
# Offer a boot-checked AMI to the AWS Marketplace listing as a new version (#3004).
#
#   TRINITY_AWS_PRODUCT_ID=<product id> \
#   TRINITY_AWS_INGESTION_ROLE_ARN=<ingestion role ARN> \
#   [TRINITY_AWS_SUBMIT=apply] \
#     bash mp-submit.sh <ami-id>
#
# Refuses an AMI with no pass record from boot-check.sh; the record names the tag.
# The change set is a dry run (Intent=VALIDATE) unless TRINITY_AWS_SUBMIT=apply.
# An applied submit removes the record. Exit 1 means AWS rejected the change set,
# 3 means AWS had no verdict within the wait (an hour). Expect about 30 minutes:
# AWS scans the AMI before it answers, on a dry run too.
#
# The product id and the ingestion role (policy AWSMarketplaceAmiIngestion,
# trusted entity AWS Marketplace) come from the seller account. The Catalog API
# is us-east-1 only. UNVERIFIED against a live listing — try it with the default
# VALIDATE intent first.
set -euo pipefail

AMI_ID="${1:-}"
if ! [[ "$AMI_ID" =~ ^ami-[0-9a-f]+$ ]]; then
    echo "usage: mp-submit.sh <ami-id>" >&2
    exit 2
fi
: "${TRINITY_AWS_PRODUCT_ID:?TRINITY_AWS_PRODUCT_ID must be set}"
: "${TRINITY_AWS_INGESTION_ROLE_ARN:?TRINITY_AWS_INGESTION_ROLE_ARN must be set}"

RECORD="${HOME}/.trinity-marketplace/${AMI_ID}.pass"
if [ ! -f "$RECORD" ]; then
    echo "[mp-submit] no pass record for ${AMI_ID}. Run boot-check.sh on it first." >&2
    exit 1
fi
VERSION="$(sed -n 's/^tag=//p' "$RECORD")"
[ -n "$VERSION" ] || { echo "[mp-submit] ${RECORD} names no tag" >&2; exit 1; }

INTENT=VALIDATE
[ "${TRINITY_AWS_SUBMIT:-}" = "apply" ] && INTENT=APPLY

# VersionTitle is the version the listing displays.
DETAILS="$(jq -cn \
    --arg ami "$AMI_ID" --arg role "$TRINITY_AWS_INGESTION_ROLE_ARN" --arg v "$VERSION" \
    '{Version: {VersionTitle: $v, ReleaseNotes: ("Trinity " + $v + ". Release notes: https://github.com/abilityai/trinity/releases/tag/" + $v)},
      DeliveryOptions: [{
        Details: {AmiDeliveryOptionDetails: {
          AmiSource: {AmiId: $ami, AccessRoleArn: $role, UserName: "ubuntu",
                      OperatingSystemName: "UBUNTU", OperatingSystemVersion: "24.04",
                      ScanningPort: 22},
          UsageInstructions: "Open https://<instance public IP>/ and enter the EC2 instance ID when asked.",
          RecommendedInstanceType: "t3a.large",
          SecurityGroups: [
            {IpProtocol: "tcp", FromPort: 443, ToPort: 443, IpRanges: ["0.0.0.0/0"]},
            {IpProtocol: "tcp", FromPort: 80, ToPort: 80, IpRanges: ["0.0.0.0/0"]}
          ]}}}]}')"

CHANGES="$(jq -cn --arg pid "$TRINITY_AWS_PRODUCT_ID" --arg d "$DETAILS" \
    '[{ChangeType: "AddDeliveryOptions",
       Entity: {Type: "AmiProduct@1.0", Identifier: $pid},
       Details: $d}]')"

echo "[mp-submit] ${INTENT}: ${AMI_ID} as version ${VERSION} of ${TRINITY_AWS_PRODUCT_ID}"
CHANGE_SET="$(aws marketplace-catalog start-change-set \
    --region us-east-1 \
    --catalog AWSMarketplace \
    --intent "$INTENT" \
    --change-set "$CHANGES" \
    --change-set-name "trinity-${VERSION}" \
    --query ChangeSetId --output text)"

# start-change-set only queues the request; AWS checks it while PREPARING.
describe() {
    aws marketplace-catalog describe-change-set --region us-east-1 --catalog AWSMarketplace \
        --change-set-id "$CHANGE_SET" --output json
}
# AWS scans a new AMI while PREPARING, dry run included: about 30 minutes on this listing.
STATE="$(describe)"
for _ in $(seq 120); do
    [ "$(jq -r .Status <<<"$STATE")" = "PREPARING" ] || break
    sleep "${TRINITY_MP_POLL:-30}"
    STATE="$(describe)"
done
STATUS="$(jq -r .Status <<<"$STATE")"
echo "[mp-submit] change set ${CHANGE_SET}: ${STATUS}"
case "$STATUS" in
    SUCCEEDED|APPLYING) ;;
    PREPARING)
        # No verdict yet is no rejection.
        echo "[mp-submit] AWS is still preparing the change set. The pass record is kept. Read the verdict with:" >&2
        echo "  aws marketplace-catalog describe-change-set --region us-east-1 --catalog AWSMarketplace --change-set-id ${CHANGE_SET}" >&2
        exit 3 ;;
    *)  jq -r '.FailureDescription // empty, (.ChangeSet[]?.ErrorDetailList[]? | "  \(.ErrorCode): \(.ErrorMessage)")' <<<"$STATE" >&2
        echo "[mp-submit] FATAL: AWS did not accept the change set. The pass record is kept." >&2
        exit 1 ;;
esac

# APPLYING means AWS accepted the version and is scanning the AMI, which takes hours.
[ "$INTENT" = "APPLY" ] && rm -f "$RECORD"
exit 0
