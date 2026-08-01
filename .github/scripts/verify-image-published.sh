#!/usr/bin/env bash
# Assert the add-on image for a version is published and pullable, for every
# architecture hactl_companion/config.yaml declares.
#
# Why this exists
# ---------------
# Home Assistant's add-on store reads `version` out of hactl_companion/config.yaml
# on the default branch, and the Supervisor pulls `<image>:<that version>`. So the
# moment the bump lands on main, every instance is told an update exists — and if
# the image for it has not been pushed yet, "Update" fails and the add-on stays on
# the old version.
#
# That was a real, repeating failure: the release workflows bumped the version on
# main, cut the tag, and only THEN triggered the Docker build, which runs its own
# test job before a two-platform buildx. Every release opened a window, minutes
# wide, in which the store advertised an image that did not exist. It was first
# written off as "the image had not finished pushing", which is the symptom, not
# the cause — the cause is that the advertisement preceded the artifact.
#
# The release workflows now publish the image BEFORE the bump reaches main, and
# call this script in between. It is the gate that makes the ordering an
# invariant rather than a comment.
#
# Deliberately anonymous
# ----------------------
# The Supervisor pulls with no credentials. Checking as an authenticated user
# would pass while a private package left every instance unable to update, so the
# token below is the registry's anonymous pull token and nothing else. Do not add
# a login here.
#
# Usage: verify-image-published.sh <version>   e.g. verify-image-published.sh 2026.7.11

set -euo pipefail

VERSION="${1:?usage: verify-image-published.sh <version>}"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
CONFIG="${REPO_ROOT}/hactl_companion/config.yaml"

[ -f "${CONFIG}" ] || { echo "::error::${CONFIG} not found"; exit 1; }

IMAGE="$(sed -n 's/^image: *"\{0,1\}\([^"]*\)"\{0,1\}$/\1/p' "${CONFIG}")"
[ -n "${IMAGE}" ] || { echo "::error::no image: line in ${CONFIG}"; exit 1; }

# The arch set is DERIVED from config.yaml, never hand-listed here: the thing
# that must be published is exactly the thing the add-on claims to support, and
# a second list would drift from the first the day someone adds an arch.
# Read with a loop rather than `mapfile`: macOS ships bash 3.2, and a gate that
# only runs on the CI runner cannot be tried before it is trusted.
ARCHES=()
while IFS= read -r a; do
  [ -n "${a}" ] && ARCHES+=("${a}")
done < <(sed -n '/^arch:/,/^[^ -]/p' "${CONFIG}" | sed -n 's/^ *- *//p')
[ "${#ARCHES[@]}" -gt 0 ] || { echo "::error::no arch: entries in ${CONFIG}"; exit 1; }

# Home Assistant add-on arch names are not OCI platform names.
oci_platform() {
  case "$1" in
    amd64)   echo "amd64" ;;
    aarch64) echo "arm64" ;;
    armv7)   echo "arm/v7" ;;
    armhf)   echo "arm/v6" ;;
    i386)    echo "386" ;;
    *)       echo "::error::unknown add-on arch %q — extend oci_platform()" >&2; return 1 ;;
  esac
}

REGISTRY="${IMAGE%%/*}"
REPOSITORY="${IMAGE#*/}"

echo "verifying ${IMAGE}:${VERSION} for: ${ARCHES[*]}"

TOKEN="$(curl -fsSL "https://${REGISTRY}/token?scope=repository:${REPOSITORY}:pull&service=${REGISTRY}" \
  | python3 -c 'import json,sys; print(json.load(sys.stdin).get("token",""))')"
[ -n "${TOKEN}" ] || { echo "::error::could not obtain an anonymous pull token for ${REPOSITORY}"; exit 1; }

MANIFEST="$(curl -fsSL \
  -H "Authorization: Bearer ${TOKEN}" \
  -H "Accept: application/vnd.oci.image.index.v1+json" \
  -H "Accept: application/vnd.docker.distribution.manifest.list.v2+json" \
  -H "Accept: application/vnd.oci.image.manifest.v1+json" \
  -H "Accept: application/vnd.docker.distribution.manifest.v2+json" \
  "https://${REGISTRY}/v2/${REPOSITORY}/manifests/${VERSION}")" || {
    echo "::error::${IMAGE}:${VERSION} is not pullable anonymously — the add-on store must not advertise this version yet"
    exit 1
  }

# Attestation manifests carry platform unknown/unknown; they are not runnable
# images and are ignored rather than counted.
PUBLISHED="$(printf '%s' "${MANIFEST}" | python3 -c '
import json, sys
d = json.load(sys.stdin)
out = []
for m in d.get("manifests") or []:
    p = m.get("platform") or {}
    os_, arch, var = p.get("os"), p.get("architecture"), p.get("variant")
    if os_ != "linux" or arch in (None, "unknown"):
        continue
    out.append(arch + ("/" + var if var else ""))
if not out and d.get("config"):
    out.append("SINGLE")     # a single-platform manifest, platform not stated here
print("\n".join(sorted(set(out))))
')"

echo "published platforms: ${PUBLISHED:-<none>}"

if [ "${PUBLISHED}" = "SINGLE" ]; then
  echo "::error::${IMAGE}:${VERSION} is a single-platform manifest; config.yaml declares ${#ARCHES[@]} arches (${ARCHES[*]})"
  exit 1
fi

missing=0
for a in "${ARCHES[@]}"; do
  want="$(oci_platform "${a}")"
  if printf '%s\n' "${PUBLISHED}" | grep -qx "${want}"; then
    echo "  ok      ${a} (${want})"
  else
    echo "::error::${IMAGE}:${VERSION} has no ${want} image, but config.yaml declares arch ${a} — instances on that arch could not update"
    missing=1
  fi
done

[ "${missing}" -eq 0 ] || exit 1
echo "${IMAGE}:${VERSION} is published for every declared arch"
