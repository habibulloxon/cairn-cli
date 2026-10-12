#!/usr/bin/env bash
# Publish a release: run the tests, build the dashboard, pack the CLI and the built UI into
# one tarball, create the GitHub release, and point the Homebrew formula at it.
#
#   1. Bump __version__ in cairn/__init__.py and commit.
#   2. scripts/release.sh
#
# Needs: gh (logged in), npm, the cairn-ui repo cloned at ./ui, and the tap
# (habibulloxon/homebrew-tap) cloned at $TAP_DIR (default: ../homebrew-tap).
set -euo pipefail
cd "$(dirname "$0")/.."

VERSION=$(python3 -c 'import cairn; print(cairn.__version__)')
TAG="v$VERSION"
NAME="cairn-$VERSION"
REPO=$(gh repo view --json nameWithOwner -q .nameWithOwner)
TAP_DIR=${TAP_DIR:-../homebrew-tap}
FORMULA="$TAP_DIR/Formula/cairn.rb"

fail() { echo "release: $*" >&2; exit 1; }
[ -z "$(git status --porcelain)" ] || fail "commit or stash your changes first"
[ -d ui/.git ] || fail "clone cairn-ui into ./ui first (see LOCAL_SETUP.md)"
[ -z "$(git -C ui status --porcelain)" ] || fail "ui/ has uncommitted changes"
[ -f "$FORMULA" ] || fail "no formula at $FORMULA (set TAP_DIR)"
git fetch -q --tags origin
! git rev-parse -q --verify "refs/tags/$TAG" >/dev/null || fail "$TAG exists; bump __version__ in cairn/__init__.py"

echo "== tests"
python3 -m unittest discover -s tests -t . -q

echo "== dashboard"
(cd ui && npm ci --silent && npm run build --silent)
UI_REV=$(git -C ui rev-parse --short HEAD)

echo "== tarball"
STAGE=$(mktemp -d)
trap 'rm -rf "$STAGE"' EXIT
mkdir -p "$STAGE/$NAME/ui"
cp -R bin cairn LICENSE README.md LOCAL_SETUP.md "$STAGE/$NAME/"
cp -R ui/dist "$STAGE/$NAME/ui/"
find "$STAGE/$NAME" -name __pycache__ -prune -exec rm -rf {} +
TARBALL="$STAGE/$NAME.tar.gz"
COPYFILE_DISABLE=1 tar -C "$STAGE" -czf "$TARBALL" "$NAME"  # no macOS ._ files
SHA=$(shasum -a 256 "$TARBALL" | cut -d' ' -f1)

echo "== GitHub release $TAG"
git tag -a "$TAG" -m "cairn $VERSION"
git push -q origin "$TAG"
gh release create "$TAG" "$TARBALL" --title "cairn $VERSION" \
  --notes "Includes the dashboard built from cairn-ui@$UI_REV.

\`\`\`
brew install habibulloxon/tap/cairn
cairn install
\`\`\`"

echo "== formula"
URL="https://github.com/$REPO/releases/download/$TAG/$NAME.tar.gz"
perl -pi -e "s|^  url \".*\"|  url \"$URL\"|; s|^  sha256 \".*\"|  sha256 \"$SHA\"|" "$FORMULA"
git -C "$TAP_DIR" commit -q -am "cairn $VERSION"
git -C "$TAP_DIR" push -q

echo "Released cairn $VERSION ($SHA)"
