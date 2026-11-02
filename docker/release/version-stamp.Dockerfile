# Stamps a deliberate release version into an image CI already built and you already
# tested. Every layer below the last one is the tested image, byte for byte; the final
# layer only rewrites frigate/version.py, so a released instance reports `0.17.3`
# instead of the `0.17.3-abc1234` that CI bakes into test builds.
#
# Used only by .github/workflows/release.yml. Build it with an EMPTY context — nothing
# here needs the repo, and the context would otherwise be uploaded for no reason:
#
#   docker buildx build -f docker/release/version-stamp.Dockerfile \
#     --build-arg SOURCE=<registry>/<repo>:sha-abc1234 \
#     --build-arg RELEASE_VERSION=0.17.3 \
#     --build-arg BASE_VERSION=0.17.3 \
#     --tag <registry>/<repo>:0.17.3 --provenance=false --push /tmp/empty

ARG SOURCE
FROM ${SOURCE}

ARG RELEASE_VERSION
ARG BASE_VERSION

# Refuse to stamp an image that was not built from this version's source. This catches
# the exact failure this file exists to prevent: a tag applied to a commit whose
# Makefile was never bumped, which would otherwise ship 0.17.2's code calling itself
# 0.17.3. A failure here fails the build, so nothing is pushed.
RUN set -eu; \
    f=/opt/frigate/frigate/version.py; \
    baked=$(sed -n 's/^VERSION = "\([^"]*\)".*/\1/p' "$f"); \
    if [ -z "$baked" ]; then \
        echo "FATAL: no VERSION found in $f"; \
        exit 1; \
    fi; \
    if [ "${baked%%-*}" != "$BASE_VERSION" ]; then \
        echo "FATAL: this image was built as '$baked', but the release asks for '$RELEASE_VERSION'."; \
        echo "       The tagged commit's Makefile must say VERSION = $BASE_VERSION."; \
        exit 1; \
    fi; \
    printf 'VERSION = "%s"\n' "$RELEASE_VERSION" > "$f"; \
    echo "stamped $f: $baked -> $RELEASE_VERSION"

# So the version can be read back without pulling the image:
#   docker buildx imagetools inspect <image> --format '{{ .Image.Config.Labels }}'
LABEL org.opencontainers.image.version="${RELEASE_VERSION}"
