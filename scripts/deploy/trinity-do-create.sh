#!/bin/bash
# Create a DigitalOcean droplet running Trinity (#2380).
#
# Runs on the OPERATOR'S machine, not on a server:
#
#     bash <(curl -fsSL https://raw.githubusercontent.com/abilityai/trinity/<tag>/scripts/deploy/trinity-do-create.sh)
#
# It asks for what it needs, creates the droplet, installs Trinity through
# `start.sh --provision` (the same installer the Packer image uses), waits until
# the site answers over HTTPS, and prints the address.
#
# Deliberately PROMPTS rather than shipping a file to edit. A guide that says
# "download this and change four lines" fails the audience this exists for, and
# it puts two secrets in a file on disk. `read -rs` keeps both of them off the
# terminal, out of shell history, and out of every file but the droplet's own
# user-data. Prompts also read fine from a pipe, so the flow stays testable:
#     printf 'pw\npw\ntoken\n\n\ny\n' | bash trinity-do-create.sh
set -euo pipefail

SIZE='s-4vcpu-8gb'      # 4 vCPU / 8 GB, $48/month — Trinity's recommended size.
IMAGE='ubuntu-24-04-x64'
DEFAULT_REGION='fra1'
DEFAULT_NAME='trinity'
TRINITY_IMAGE_TAG="${TRINITY_IMAGE_TAG:-v0.9.5-rc2}"

fail() { printf '\n%s\n\n' "$1" >&2; exit 1; }
ask()  { printf '%s' "$1" >&2; }

# ---------------------------------------------------------------------------
# Checks first, questions second: never collect two secrets and THEN discover
# the tool is missing.
# ---------------------------------------------------------------------------
command -v doctl >/dev/null 2>&1 || fail \
"doctl is not installed. Install it, then run this again:
    macOS:  brew install doctl
    other:  https://docs.digitalocean.com/reference/doctl/how-to/install/"

doctl account get >/dev/null 2>&1 || fail \
"doctl is installed but not signed in to DigitalOcean. Create an API token at
    https://cloud.digitalocean.com/account/api/tokens
(give it Write access), then run:
    doctl auth init"

# Where the droplet's setup file will live is a doctl question, not a taste
# question, and it is decided HERE because one possible answer is "nowhere".
# On most Linux distributions doctl installs from snap, and a snap runs inside
# its own mount namespace with a PRIVATE /tmp — so a file written to the real
# /tmp is simply not there when doctl opens it:
#     Error: open /tmp/trinity-user-data.XXXXXX: no such file or directory
# ...on a file that demonstrably exists. Snap's `home` interface can read
# non-hidden paths under $HOME, so a snap doctl gets the file there instead —
# visible name deliberately, since that interface denies dotfiles as well.
_doctl_bin="$(command -v doctl)"
case "$_doctl_bin:$(readlink -f "$_doctl_bin" 2>/dev/null)" in
    /snap/*|*:/snap/*|*:*/snapd/*|*:/usr/bin/snap) USER_DATA_DIR="${HOME:-}" ;;
    *)                                             USER_DATA_DIR="${TMPDIR:-/tmp}" ;;
esac
# A snap doctl with no usable HOME has nowhere readable to put the file; /tmp is
# invisible to it. Refuse now, before the operator has typed two secrets.
[ -n "$USER_DATA_DIR" ] || fail \
"doctl is installed as a snap but \$HOME is not set, so there is nowhere it can
read the droplet's setup file from. Run this from a normal login shell, or
install doctl from a package instead of snap."

cat >&2 <<'INTRO'

  Trinity on DigitalOcean
  ───────────────────────────────────────────────────────────────────────
  Four questions, then about six minutes of waiting. Nothing is written to
  this computer.

INTRO

# --- 1. The Trinity sign-in password ----------------------------------------
# Asked twice: this is the credential for a server that does not exist yet, so
# a typo is not recoverable by "try again" — it is a rebuild.
while :; do
    ask "  Choose a password to sign in to Trinity with (12+ characters): "
    read -rs ADMIN_PASSWORD; printf '\n' >&2
    if [ "${#ADMIN_PASSWORD}" -lt 12 ]; then
        printf '  Too short — 12 characters or more.\n\n' >&2
        continue
    fi
    case "$(printf '%s' "$ADMIN_PASSWORD" | tr '[:upper:]' '[:lower:]')" in
        password*|admin*|trinity*|changeme*|letmein*)
            printf '  Too guessable — Trinity rejects passwords starting like that.\n\n' >&2
            continue ;;
    esac
    ask "  Type it again to confirm: "
    read -rs _confirm; printf '\n' >&2
    [ "$ADMIN_PASSWORD" = "$_confirm" ] && break
    printf '  Those did not match.\n\n' >&2
done
unset _confirm
printf '  Your username will be: admin\n\n' >&2

# --- 2. The Claude subscription token ---------------------------------------
cat >&2 <<'TOKENHELP'
  Trinity's agents sign in to Claude with a subscription token. In another
  terminal run:

      claude setup-token

  and copy the sk-ant-oat01-... value it prints. (An API key, sk-ant-api03-...,
  is a different thing and will not work.)

TOKENHELP
while :; do
    ask "  Paste the token: "
    read -rs CLAUDE_SUBSCRIPTION_TOKEN; printf '\n' >&2
    case "$CLAUDE_SUBSCRIPTION_TOKEN" in
        sk-ant-oat01-*) break ;;
        sk-ant-api03-*) printf '  That is an API key, not a subscription token. Run: claude setup-token\n\n' >&2 ;;
        '')             printf '  Nothing pasted.\n\n' >&2 ;;
        *)              printf '  That does not look like a setup token — it should start sk-ant-oat01-\n\n' >&2 ;;
    esac
done
printf '\n' >&2

# --- 3 + 4. Placement -------------------------------------------------------
ask "  Which region? fra1 Frankfurt, nyc3 New York, sfo3 San Francisco, lon1 London, sgp1 Singapore [${DEFAULT_REGION}]: "
read -r REGION; REGION="${REGION:-$DEFAULT_REGION}"
ask "  What should it be called in your DigitalOcean account? [${DEFAULT_NAME}]: "
read -r DROPLET_NAME; DROPLET_NAME="${DROPLET_NAME:-$DEFAULT_NAME}"

cat >&2 <<CONFIRM

  ───────────────────────────────────────────────────────────────────────
  About to create:  ${DROPLET_NAME}  (${SIZE}, ${REGION})
  Trinity release:  ${TRINITY_IMAGE_TAG}
  This costs about \$48/month until you destroy it.

CONFIRM
ask "  Create it? [y/N]: "
read -r _go
case "$_go" in y|Y|yes|YES) ;; *) fail "Nothing was created." ;; esac

# ---------------------------------------------------------------------------
# The script the droplet runs on its first boot.
# ---------------------------------------------------------------------------
# umask before the file exists, not after: mktemp would otherwise create it
# group-readable for the instant between creation and chmod, and it carries
# both secrets.
umask 077
# A full path template, not `mktemp -t NAME`: `-t` takes a bare prefix on
# macOS/BSD but GNU coreutils requires the trailing X's and dies with
# "too few X's in template" — which is every Linux operator running this.
# USER_DATA_DIR was chosen in the pre-flight block (snap doctl → $HOME); umask
# 077 above still makes the file 0600, and the EXIT trap still removes it.
USER_DATA="$(mktemp "${USER_DATA_DIR%/}/trinity-user-data.XXXXXX")"

# Both secrets are interpolated into single-quoted shell assignments below, so a
# single quote INSIDE either one closes the string early and the droplet's first
# boot dies on a syntax error — after the droplet exists and is billing, with the
# operator watching a 15-minute progress bar that ends in a timeout. Recovery is
# a rebuild, and the cause is invisible without reading the install log.
#
# Not hypothetical: the password rules demand a special character and `'` is one,
# so `Tr0ub4dor's!Horse` passes this script's own check AND the backend's, then
# writes
#     export ADMIN_PASSWORD='Tr0ub4dor's!Horse'
# which is a syntax error.
#
# `'\''` is the portable idiom: close the string, an escaped quote, reopen.
# The image tag comes from the operator's environment and gets the same
# treatment, so the rule is simply: every '${…}' in the heredoc is a _Q value.
_shquote() { printf '%s' "$1" | sed "s/'/'\\\\''/g"; }
ADMIN_PASSWORD_Q="$(_shquote "$ADMIN_PASSWORD")"
CLAUDE_SUBSCRIPTION_TOKEN_Q="$(_shquote "$CLAUDE_SUBSCRIPTION_TOKEN")"
TRINITY_IMAGE_TAG_Q="$(_shquote "$TRINITY_IMAGE_TAG")"
trap 'rm -f "$USER_DATA"' EXIT

cat > "$USER_DATA" <<USERDATA
#!/bin/bash
set -euo pipefail
export ADMIN_PASSWORD='${ADMIN_PASSWORD_Q}'
export TRINITY_IMAGE_TAG='${TRINITY_IMAGE_TAG_Q}'
exec > >(tee -a /var/log/trinity-install.log) 2>&1
echo "=== Trinity install: \$(date -u +%FT%TZ) tag=\${TRINITY_IMAGE_TAG} ==="

apt-get update -q
apt-get install -y -q git curl jq
git clone --depth 1 --branch "\${TRINITY_IMAGE_TAG}" \\
    https://github.com/abilityai/trinity.git /opt/trinity
cd /opt/trinity

# Docker, Caddy, the firewall, a Let's Encrypt certificate for this droplet's
# own IP, then the install itself. Same code path as the 1-Click image.
./scripts/deploy/start.sh --provision --cloud digitalocean --hosted --unattended

# Hand the Claude subscription to the agents this install just created. Agents
# created later auto-assign it themselves (#74).
API=http://localhost:8000
AUTH="\$(curl -fsS -X POST "\$API/api/token" \\
    --data-urlencode "username=admin" \\
    --data-urlencode "password=\${ADMIN_PASSWORD}" | jq -r .access_token)"
curl -fsS -X POST "\$API/api/subscriptions" \\
    -H "Authorization: Bearer \$AUTH" -H 'Content-Type: application/json' \\
    -d "\$(jq -n --arg t '${CLAUDE_SUBSCRIPTION_TOKEN_Q}' \\
          '{name:"claude-subscription", token:\$t}')" >/dev/null
for agent in \$(curl -fsS "\$API/api/agents" -H "Authorization: Bearer \$AUTH" \\
               | jq -r '.[].name? // empty'); do
    curl -fsS -X PUT \\
        "\$API/api/subscriptions/agents/\${agent}?subscription_name=claude-subscription" \\
        -H "Authorization: Bearer \$AUTH" >/dev/null
    echo "Claude subscription attached to agent: \${agent}"
done

echo "=== Trinity is ready at https://\$(cat /etc/trinity/public-ip) ==="
USERDATA

# Attach whatever SSH keys the account already has. A droplet nobody can get a
# shell on is a droplet nobody can support: without this, DigitalOcean emails a
# root password to the account owner and the only way in is the browser console.
# Keys already on the account are the operator's own, and this is their account
# and their server — it does not create, upload or generate anything.
SSH_KEYS="$(doctl compute ssh-key list --format ID --no-header 2>/dev/null | tr '\n' ',' | sed 's/,$//')"
SSH_ARGS=()
[ -n "$SSH_KEYS" ] && SSH_ARGS=(--ssh-keys "$SSH_KEYS")
# Expanded below as ${SSH_ARGS[@]+"${SSH_ARGS[@]}"}: under `set -u`, bash < 4.4
# (macOS's /bin/bash is 3.2) calls an EMPTY array unbound, so an account with no
# SSH keys died here, after both secrets had been typed.

printf '\n  Creating the droplet...\n' >&2
doctl compute droplet create "$DROPLET_NAME" \
    --image "$IMAGE" --size "$SIZE" --region "$REGION" \
    --user-data-file "$USER_DATA" \
    ${SSH_ARGS[@]+"${SSH_ARGS[@]}"} \
    --wait --format ID,Name,PublicIPv4 --no-header >&2

IP="$(doctl compute droplet list "$DROPLET_NAME" \
      --format PublicIPv4 --no-header | tail -1 | tr -d '[:space:]')"
[ -n "$IP" ] || fail "The droplet was created but has no public IP yet. Check https://cloud.digitalocean.com/droplets"

printf '\n  Server is up at %s. Installing Trinity — about five more minutes.\n  ' "$IP" >&2

# Polled with normal certificate verification, against the real address: a pass
# here means the certificate validated too, not merely that something answered.
DEADLINE=$(( $(date +%s) + 900 ))
while [ "$(date +%s)" -lt "$DEADLINE" ]; do
    if curl -fsS -o /dev/null --max-time 10 "https://${IP}/" 2>/dev/null; then
        cat >&2 <<DONE


  ───────────────────────────────────────────────────────────────────────
  Trinity is ready.

      Open:     https://${IP}
      Username: admin
      Password: the one you chose above

DONE
        exit 0
    fi
    printf '.' >&2
    sleep 15
done

cat >&2 <<SLOW


  Trinity did not finish within 15 minutes. It may still be working — try
  https://${IP} in a browser first.

  If it does not answer, open the droplet's Console from
  https://cloud.digitalocean.com/droplets (the "Console" button — a terminal
  in your browser, no SSH key needed) and run:

      tail -50 /var/log/trinity-install.log

SLOW
exit 1
