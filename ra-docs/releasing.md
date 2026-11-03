# Releasing a new version

How to ship a build of `dev-fork` as a numbered release, how the version number is
chosen, and what to do when a release fails. The workflows involved are
`.github/workflows/release.yml` (**Release**) and `.github/workflows/bump_version.yml`
(**Bump version**).

## How versions work

The `VERSION` line in the `Makefile` on `dev-fork` is the version being **worked
toward** — the next one to be released. Do not edit it by hand: the workflows move it.

| What happened | Instances report | Image tags in ECR |
|---|---|---|
| Push to any branch | `0.17.5-abc1234` | `sha-abc1234` |
| Push to `dev-fork` | `0.17.5-abc1234` | `sha-abc1234`, `0.17.5-abc1234`, `latest` |
| Release of that commit | `0.17.5` | `0.17.5`, and `stable` if it is the newest stable release |

The `-abc1234` suffix marks a test build; a bare version marks a release. The version
is shown on the System page and at `/api/version`.

A typical cycle:

1. The Makefile says `0.17.5`. Everything merged into `dev-fork` builds as `0.17.5-<sha>`.
2. You test one of those builds and release it. Devices on it now report `0.17.5`.
3. The release moves the Makefile to `0.17.6`, so the next merge builds as `0.17.6-<sha>`.

## Release a new version

Before you start:

- the work is merged into `dev-fork`, and CI on `dev-fork` has finished green
- you have tested the `:<version>-<sha>` image you want to ship

There are two ways in. Both run the same pipeline and end in the same place.

### Option A: run the Release workflow (recommended)

1. **Actions** → **Release** → **Run workflow**.
2. Leave **Use workflow from** on `dev-fork`.
3. **Run workflow**.

There is nothing to type: the version comes from the Makefile. The pipeline creates
the tag and the GitHub release (title `v0.17.5`, generated notes) — edit the title or
notes afterwards if you like, e.g. `v0.17.5 - Faster timeline`.

This releases the **tip** of `dev-fork`. If something was merged after the build you
tested, the tip is not what you tested: either test the tip, or use option B and pick
the tested commit as the target.

### Option B: publish a GitHub release first

1. **Releases** → **Draft a new release**.
2. **Choose a tag** → type `v` + the version from the Makefile on `dev-fork`, e.g.
   `v0.17.5` (it is also the tested image's tag without `-<sha>`) → **Create new tag
   on publish**.
3. **Target**: `dev-fork`, or the exact commit you tested under **Recent commits**.
4. Write the title and notes, or use **Generate release notes**.
5. **Publish release**. Publishing creates the tag, which starts **Release** on its
   own — watch it under **Actions**. Your release text is kept as it is.

If the version you typed does not match the Makefile, the pipeline fails before
shipping anything. Delete the release and its tag, then publish again with the right
version.

Pushing a tag from git — `git tag v0.17.5 <sha> && git push origin v0.17.5` — works
the same way; the pipeline then creates the GitHub release itself.

### What the pipeline does

1. **Checks** the tag matches the Makefile at that commit, the commit is on `dev-fork`,
   CI built an image for it, and (option A) the version is not released already.
2. **Stamps** the tested image: one added layer rewrites `frigate/version.py` to the
   bare version; every other layer is the image you tested. Pushes `:<version>`, and
   `:stable` if this is the newest stable release.
3. **Tags** the commit `v<version>` (option A only — in option B the tag already exists).
4. **Creates the GitHub release**, unless one exists. It is marked **Latest** only if it
   is the newest stable release; a pre-release is marked as such.
5. **Moves the Makefile** on `dev-fork` to the next patch version (stable releases only).

The run's summary lists the image, the tag, the release and the new Makefile version.

## Choosing the version

**Patch** (`0.17.5` → `0.17.6`): automatic after every stable release. Nothing to do.

**Minor** (`0.17.6` → `0.18.0`) **or major** (`0.17.6` → `1.0.0`): before releasing,

1. **Actions** → **Bump version** → **Run workflow**, **Use workflow from** `dev-fork`.
2. Pick `minor` or `major` → **Run workflow**.
3. CI builds `0.18.0-<sha>`. Test that, then release as above.

Skipping a version is fine — `0.17.6` above was never released. The `patch` option in
**Bump version** is only for recovery, when a release could not move the Makefile on.

**Pre-release** (beta, rc): use option B with a tag like `v0.18.0-beta1` while the
Makefile says `0.18.0`. It is pushed as `:0.18.0-beta1` and published as a GitHub
pre-release; it does not take `:stable` and does not move the Makefile. Follow with
`v0.18.0-beta2` and so on, then release `0.18.0` normally.

## Deploying

The pipeline does not touch devices. Point each device's compose file at the new tag
(`…/frigate:0.17.5`), then `docker compose pull && docker compose up -d`. The System
page should show `0.17.5`. To roll back, point it at the previous version tag.

Devices on `:stable` could update themselves with an auto-updater such as Watchtower,
but ECR logins expire after 12 hours, so the updater needs the ECR credential helper —
and a bad release then reaches every device at once.

## When a release fails

Any failure before the **Create the release tag** step leaves nothing behind: fix the
cause and use **Re-run all jobs** on the failed run. If an option A run failed at or
after that step, the tag exists, so instead run **Release** again with **Use workflow
from** set to that tag: it finishes whatever is missing — the GitHub release, the
Makefile bump — and re-pushes the same image.

| The run says | Meaning | Fix |
|---|---|---|
| `Tag vX is on a commit whose Makefile says VERSION = Y` | The version typed in option B, or a pushed tag, does not match the Makefile | Delete the GitHub release and the tag, then release `vY` |
| `… is not on dev-fork` | The tag or target commit is not on `dev-fork` | Delete the tag (and release); tag a commit on `dev-fork` |
| `No image tagged sha-…` | CI has not finished, or failed, for that commit | Wait for CI on `dev-fork`, then re-run |
| `vX is already released, but the Makefile … still says X` | The previous release failed partway, or could not move the Makefile on | Run **Release** from the `vX` tag; if it had finished, **Bump version** → `patch` |
| `Released vX, but could not push the bump` | Shipped fine; the Makefile push was blocked, e.g. by branch protection | **Bump version** → `patch`; if protection blocks that too, change `VERSION` in a pull request |
| `Run this with 'Use workflow from' set to dev-fork …` | Started from another branch | Run again from `dev-fork` |

To delete a tag: **Tags** → the tag → **Delete**, or
`git push origin :refs/tags/v0.17.5`. Delete its GitHub release first.

## Rules

- Never move or reuse a released tag. Devices pinned to `:0.17.5` would get different
  bytes under the same name.
- In **Use workflow from**, pick only `dev-fork`, or a tag newer than `v0.17.4`.
  GitHub runs the workflow as it was at that ref; older tags carry older pipelines, and
  `v0.17.3` and earlier would move `:stable` back to that old image.
- `v17.1-update-with-adaptive` takes no pushes: its own CI still treats it as the
  release branch and would move `latest`.
- **Release** pushes the Makefile bump to `dev-fork` as `github-actions[bot]`. Branch
  protection that requires pull requests on `dev-fork` blocks that push; blocking
  force-pushes and deletion does not.
