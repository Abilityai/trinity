#!/bin/bash
# Offer the AMI Packer just built to the AWS Marketplace listing as a new
# version (#3004, the release runbook). Runs as a `shell-local` post-processor
# after the `manifest` post-processor.
#
# NO-OP BY DEFAULT: exits 0 unless TRINITY_AWS_PRODUCT_ID is set, so a plain
# `packer build` only builds. VALIDATE by default: the change set is a dry run
# unless TRINITY_AWS_SUBMIT=apply. Opt-in per invocation:
#
#   TRINITY_AWS_PRODUCT_ID=<product id> \
#   TRINITY_AWS_INGESTION_ROLE_ARN=<ingestion role ARN> \
#   TRINITY_AWS_SUBMIT=apply \
#     packer build -var "image_tag=<release tag>" trinity.pkr.hcl
#
# The product id and the ingestion role (policy AWSMarketplaceAmiIngestion,
# trusted entity AWS Marketplace) come from the seller account. The Catalog API
# is us-east-1 only. UNVERIFIED against a live listing — try it with the default
# VALIDATE intent first.
set -euo pipefail

MANIFEST="${TRINITY_PACKER_MANIFEST:-manifest.json}"

if [ -z "${TRINITY_AWS_PRODUCT_ID:-}" ]; then
    echo "[mp-submit] TRINITY_AWS_PRODUCT_ID unset — AMI built, not submitted."
    exit 0
fi
: "${TRINITY_AWS_INGESTION_ROLE_ARN:?TRINITY_AWS_INGESTION_ROLE_ARN must be set to submit}"
[ -f "$MANIFEST" ] || { echo "[mp-submit] FATAL: $MANIFEST not found." >&2; exit 1; }

# artifact_id is "<region>:<ami id>".
AMI_ID="$(jq -r '.builds[-1].artifact_id | split(":")[1]' "$MANIFEST")"
case "$AMI_ID" in
    ami-*) ;;
    *) echo "[mp-submit] FATAL: could not read an AMI id from $MANIFEST." >&2; exit 1 ;;
esac

INTENT=VALIDATE
[ "${TRINITY_AWS_SUBMIT:-}" = "apply" ] && INTENT=APPLY
VERSION="${TRINITY_IMAGE_TAG:?TRINITY_IMAGE_TAG must be set}"

DETAILS="$(jq -cn \
    --arg ami "$AMI_ID" --arg role "$TRINITY_AWS_INGESTION_ROLE_ARN" --arg v "$VERSION" \
    '{Version: {VersionTitle: $v, ReleaseNotes: ("Trinity " + $v)},
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
aws marketplace-catalog start-change-set \
    --region us-east-1 \
    --catalog AWSMarketplace \
    --intent "$INTENT" \
    --change-set "$CHANGES" \
    --change-set-name "trinity-${VERSION}"
