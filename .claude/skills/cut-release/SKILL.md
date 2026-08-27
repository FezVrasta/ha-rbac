---
name: cut-release
description: >-
  Cut a new tagged release of the ha-rbac integration. Use when asked to
  "release", "cut a release", "ship a version", or publish a new vX.Y.Z. Covers
  the version scheme, the fact that manifest.json is bumped automatically by CI
  (never by hand), the commit and release-notes conventions, and how to verify
  the release landed.
---

# Cutting a release of ha-rbac

A release is a GitHub release with a `vX.Y.Z` tag. Publishing it triggers
`.github/workflows/release.yml`, which sets `manifest.json`'s version from the
tag and commits it as `github-actions[bot]`. That is the whole delivery
mechanism — HACS serves the tagged commit.

## The one thing that trips people up

**Do not edit `custom_components/ha_rbac/manifest.json`'s `version` yourself.**
The release workflow does it from the tag name (`v0.13.0` → `0.13.0`) and pushes
a `Set version to vX.Y.Z` commit afterwards. If you bump it by hand you get a
duplicate/conflicting change. Your job is to commit the *content* and create the
*release*; the version follows the tag.

## Version scheme

- Tags are v-prefixed semver: `v0.13.0`.
- Still `0.x` (alpha). A normal change — feature or fix — is a **minor** bump
  (`0.12.0` → `0.13.0`). Reserve a patch bump for a same-day follow-up to a
  release that shipped broken.
- Check the last tag first: `git tag --sort=-creatordate | head -1`.

## Steps

1. **Be releasable.** On `main`, working tree clean-ish, and green:
   ```bash
   .venv/bin/python -m pytest -q
   uvx ruff check . && uvx ruff format --check .
   ```
   CI runs pytest, ruff, hassfest and the HACS action on every push; don't ship
   red.

2. **Verify behavioural changes on the VM.** If the release changes enforcement,
   deploy the changed files to the test instance and re-run the relevant check
   before tagging — a green suite is not the finish line for this project. The
   test box is the OrbStack machine `hassio13`; the integration lives in the
   `homeassistant` container at `/config/custom_components/ha_rbac`, mounted on
   the host at `/var/lib/homeassistant/homeassistant/custom_components/ha_rbac`.
   Deploy a file and restart:
   ```bash
   cat custom_components/ha_rbac/decide.py | \
     orb -m hassio13 bash -c 'sudo tee /var/lib/homeassistant/homeassistant/custom_components/ha_rbac/decide.py >/dev/null'
   orb -m hassio13 bash -c 'sudo docker restart homeassistant'   # ~15s to rebind
   ```
   A config-entry reload is not enough — Python caches the imported modules, so
   code changes need a full HA restart to take effect.

   **Frontend changes need the manifest version bumped too, or you are testing
   the old panel.** `__init__.py` registers the panel as
   `ha-rbac-panel.js?v={integration.version}`, so the browser keys its cache on
   the version in `manifest.json` — which on the VM is whatever was last
   *installed* there, not what you just deployed. Deploying the JS alone leaves
   the URL unchanged and the browser serves the cached copy forever. A restart
   does not help; neither does a normal reload. Bump it before testing:
   ```bash
   orb -m hassio13 bash -c 'sudo python3 -c "
   import json; p=\"/var/lib/homeassistant/homeassistant/custom_components/ha_rbac/manifest.json\"
   d=json.load(open(p)); d[\"version\"]=\"0.0.0-test\"; json.dump(d,open(p,\"w\"),indent=2)"'
   ```
   Then confirm what is actually being served before believing any UI result:
   ```bash
   orb -m hassio13 bash -c 'sudo grep -c "<a string only your change contains>" \
     /var/lib/homeassistant/homeassistant/custom_components/ha_rbac/frontend/ha-rbac-panel.js'
   ```
   This has burned a whole debugging session: a panel fix was deployed, restarted
   and declared broken three times while the browser ran a build from `0.11.0`.

   **To drive the panel in a browser, mint a token — do not try to log in.** The
   owner's refresh tokens are in `.storage/auth`; exchange one for an access
   token and write `hassTokens` into the origin's `localStorage`. The `clientId`
   must match the origin you are browsing (`http://192.168.139.96:8123/`), not
   `127.0.0.1`, or the frontend rejects it. Note the panel refuses
   `ha_rbac/roles/list` for any non-admin, so a Guest session shows an error card
   and no editor — check which user the session belongs to before concluding
   anything about the UI.

3. **Commit the content.** One commit per logical change, present-tense summary
   line, and a body that says *why* — match the surrounding `git log`, which is
   discursive and explains the reasoning, not just the diff. End every commit
   with the trailer:
   ```
   Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
   ```
   Then `git push origin main`.

4. **Create the release.** Write notes to a file and:
   ```bash
   gh release create vX.Y.Z --title "vX.Y.Z" --notes-file /tmp/release-X.Y.Z.md
   ```
   Notes tone: plain and direct, lead with the essence in one or two lines, use
   `##` sections (e.g. "Fix", "…"), and close with the standing alpha line —
   currently: *"Still an alpha … Try it on something that isn't your front
   door."* Read the previous release for the voice: `gh release view <lasttag>`.

5. **Verify it landed.**
   ```bash
   gh run list -L 5           # the "Release" run should be success
   git fetch origin main && git show origin/main:custom_components/ha_rbac/manifest.json | grep version
   ```
   The manifest should read the new version, committed by `github-actions[bot]`.
   Then `git pull --ff-only` so local main includes that bump.

## Notes

- Releases go out from `main` directly — that is this project's established
  workflow (every prior release tag points at a `main` commit). No release
  branch.
- **Never amend, squash, or rebase a commit already pushed to a PR branch** (see
  the repo's AI/contribution guidance) — history is meant to be followed.
- If CI on the tagged commit fails after the release is out, fix forward with a
  patch release rather than deleting the tag.
