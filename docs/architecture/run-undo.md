# Run undo

`pico undo --run <run_id>` restores workspace changes made by one completed agent
run without resetting the repository or discarding changes that existed before the
run.

## Lifecycle

```text
risky tool approved
  -> stage target paths (file tools) or workspace scope (shell)
  -> execute tool
  -> compare staged and current path states
  -> retain first preimages only for changed paths
  -> update each path's expected post-state
  -> expose undo availability in report.json

pico undo
  -> load and validate the run manifest
  -> reject incomplete journals and unsafe paths
  -> compare every current path with its expected post-state
  -> reject the entire operation on any conflict
  -> restore files, symlinks, directories, contents, and modes
  -> verify every restored path
  -> update report.json undo summary
```

The first preimage is the actual workspace state immediately before Pico first
changes a path in that run. It may therefore be an unstaged or untracked user file;
Git `HEAD` is not used as a restoration source.

## Manifest

Each run owns `undo/manifest.json` and content-addressed `undo/blobs/<sha256>`
files. An entry records:

- the original kind and state (`absent`, regular file, symlink, or directory);
- regular-file SHA-256, byte size, mode, and blob reference;
- the expected kind, digest, target, and mode after the last tool touching it;
- first and most recent record timestamps.

Temporary full-workspace preimages for shell calls live under `undo/pending/` only
until the changed paths are known. A remaining pending snapshot marks the journal
incomplete and blocks restoration.

## Conflict policy

Restoration is fail-closed and preflights every active entry before writing:

- current state equals expected post-state: restore it;
- current state already equals the original: leave it unchanged;
- anything else: report the path as a conflict and restore nothing.

Created directories receive an additional descendant check so a file added after
the run cannot be deleted indirectly. Blob hashes and final restored states are
verified. A second undo after success is an idempotent no-op.

## Scope

The journal excludes runtime and protected paths already outside Pico's workspace
diff scope: `.git`, `.pico`, virtual environments, and common Python caches. It is
a local single-run recovery mechanism, not a backup system or a replacement for
code review. It deliberately does not create commits, change refs, rewrite the Git
index, or invoke `git reset --hard`.
