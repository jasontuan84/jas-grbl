# Releasing jasGrbl

How to cut a new versioned release and publish it to GitHub.

## Prerequisites (one-time)

- [GitHub CLI](https://cli.github.com/) installed:
  ```powershell
  winget install --id GitHub.cli -e
  ```
- Authenticated (stored in the keyring, so this is a one-time step):
  ```bash
  gh auth login          # GitHub.com  →  HTTPS  →  Login with a web browser
  gh auth status         # confirm you are logged in
  ```

## Release steps

### 1. Bump the version

The version comes from a single source of truth:

```
jasgrbl_pkg/__init__.py   →   __version__ = "X.Y.Z"
```

Update it there. The badge in `README.md` is hardcoded, so bump that too if you
want it to match.

### 2. Update the changelog

Move the `## [Unreleased]` notes into a new `## [X.Y.Z]` section in
[`CHANGES_LOG.md`](CHANGES_LOG.md).

### 3. Commit and push

```bash
git add -A
git commit -m "Release vX.Y.Z"
git push origin main
```

The release tag is created from whatever commit is at the tip of `main`, so make
sure everything is committed and pushed first.

### 4. Build the release zip

```bash
python tools/package.py
```

Produces `releases/jasGrbl-X.Y.Z.zip` (version taken from `__version__`). The zip
has the `.inx` files at its root — the format Inkscape's Extension Manager expects
for **Install Package** — plus an `INSTALL.txt` with end-user instructions.

### 5. Create the GitHub release

```bash
gh release create vX.Y.Z releases/jasGrbl-X.Y.Z.zip \
  --title "jasGrbl vX.Y.Z" \
  --notes-file path/to/release-notes.md
```

Notes:
- Use `--notes-file` for a written summary, or `--generate-notes` to auto-build
  notes from merged commits/PRs.
- Add `--draft` to review the release on the web before publishing.
- Add `--prerelease` for a beta/RC.

Write the release notes as a short summary (highlights + install snippet + a link
to the full changelog), not a copy of the whole `CHANGES_LOG.md`.

## Fixing a release after publishing

```bash
# Edit title/notes
gh release edit vX.Y.Z --title "..." --notes-file notes.md

# Replace or add an asset
gh release upload vX.Y.Z releases/jasGrbl-X.Y.Z.zip --clobber

# Delete the release (and optionally the tag)
gh release delete vX.Y.Z --cleanup-tag
```

## Quick reference

```bash
# after bumping __version__ and updating CHANGES_LOG.md
git add -A && git commit -m "Release vX.Y.Z" && git push origin main
python tools/package.py
gh release create vX.Y.Z releases/jasGrbl-X.Y.Z.zip --title "jasGrbl vX.Y.Z" --notes-file notes.md
```
