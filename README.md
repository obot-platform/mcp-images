# MCP Images

This repository builds and publishes the container images used by the Obot MCP
catalog. Image publication is coordinated by GitHub Actions and
`scripts/image_plan.py`.

## Image families

Images are divided into three families:

- **Repackages** are configured in `repackaging/images.yaml`. Node and Python
  packages are installed into an application base and then wrapped with MMMCP.
  Docker repackages use an upstream image directly.
- **Repository images** are versioned images configured in
  `repository-images.yaml`, such as GitHub and Tableau.
- **Utilities** are unversioned images configured in `repository-images.yaml`,
  such as the STDIO wrapper and HTTP webhook converter.

## Immutable revisions

Versioned images are published as `{version}-obotN`, where `N` is an
auto-incrementing revision. Before a build, the planner lists the repository's
existing tags, finds the highest exact revision for that version, and allocates
the next number. A new version starts at `obot1`.

For example, if `1.2.3-obot1` and `1.2.3-obot2` exist, the next build publishes
`1.2.3-obot3`. Existing revision tags are never intentionally overwritten, and
the workflow checks again that the selected tag is absent before building.
There is no moving `1.2.3` or `1.2.3-main` alias; catalog updates point directly
to the immutable revision.

Revision allocation answers *where to publish*, not *whether the image contents
changed*. Any triggered build of a versioned image receives the next revision.

## Selecting affected images

On a push to `main`, the coordinator compares the previous and current Git
revisions. An image is selected when its manifest entry or an owned path changes.
Shared build inputs select their known dependents:

- Node base or Node repackage changes select Node repackages.
- Python base or Python repackage changes select Python repackages.
- MMMCP wrapper changes select Node and Python repackages.
- Docker repackage changes select Docker repackages.
- Common planner, publisher, or Docker context changes select all applicable
  images.

An unrelated change selects no images. Changing only one manifest entry, such
as DuckDuckGo, selects only that image.

Pull requests use the same selector and write a read-only table of proposed
immutable tags to the Actions job summary. The preview does not reserve those
tags, so the final revision may change before merge.

## Parent images

Builds resolve configurable parent images to digests before publication. This
keeps each individual build on one exact parent even when its configured tag is
mutable.

The shared Node and Python bases use `cgr.dev/chainguard/wolfi-base:latest`
directly. The root `MMMCP_IMAGE` file is the single source for the complete
MMMCP image reference used by CI. The planner passes that reference to every
MMMCP consumer without resolving it to a digest. Their Dockerfiles default to
`mmmcp:latest` for direct builds.
Repository-image parent versions are similarly defined in
`repository-images.yaml`; their Dockerfile arguments use moving defaults such
as `latest` or `node:alpine` and CI overrides them with digest-pinned manifest
references.

## Manual rebuilds

The **Publish MCP Images** workflow can be dispatched from `main` in one of two
modes:

- `single-repackage` rebuilds one named repackage.
- `all-mmmcp-consumers` rebuilds every Node and Python repackage and every
  repository or utility Dockerfile with an `MMMCP_IMAGE` argument.

A manual run intentionally rebuilds the selected images and allocates their next
immutable revisions. Changing the configured MMMCP version automatically
selects all consumers; the all-consumers mode can rebuild them without a change.

Manual runs do not compare parent digests with the previous revision and do not
skip unchanged builds. Single-image targeting applies only to repackages; the
MMMCP mode also includes matching repository images and utilities.

## Utilities and releases

Utilities normally publish moving branch tags. A repository `v*` tag publishes
that exact immutable utility tag; a non-release-candidate tag also updates
`latest`. Retrying the same source revision reuses its existing release tag.

## Security scans

Published images are signed, receive an SBOM, and are scanned with Trivy.
Changes to the scanning workflows or security policy trigger scan-only jobs.
These jobs resolve the highest existing revision and do not build an image or
allocate a new revision.

## Workflow map

- `.github/workflows/build.yml` tests the planner, validates Dockerfiles, and
  previews affected revisions on pull requests.
- `.github/workflows/publish-images.yml` selects affected images and coordinates
  base builds, publication, and rescans.
- `.github/workflows/publish-image.yml` plans, builds, signs, scans, and updates
  the catalog for selected images.
- `.github/workflows/base-images.yml` publishes the shared Node and Python base
  images.
- `.github/workflows/scan-images.yml` and
  `.github/workflows/scan-base-images.yml` perform scan-only runs.
