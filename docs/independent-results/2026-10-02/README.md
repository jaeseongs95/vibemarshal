# Independent VM results — 2026-10-02 KST

Evidence-only branch based on exact commit `32bb0f9dd9f024045d24487312b50f5b703573a3`, tree `27e18c0c7853c1d35a98d5437cebf0ba703ed42d`. Work occurred on 2026-10-01 UTC / 2026-10-02 KST. All additions are reports, synthetic diagnostic runners, logs, manifests, or inert proposal diffs. Product source and operational state are unchanged.

## Read first

- [Linux portable and installed public-path results](vm-evidence/report.md): 118 explicitly selected tests passed with zero failures/errors/skips; nine installed CLI subprocess checks and installed-candidate probe/pre-provider dry run passed. Initial failures and exclusions are retained. The dry run keeps `release_pass=false`. Windows owner/restart and full product qualification are not established.
- [SQLite baseline and initial proposal evidence](vm-sqlite/REPORT.md): 26 new boundary tests passed; 20 concurrency executions repeat two existing tests. Partial-initialization and other diagnostic findings are preserved. Revision 1 is blocked by an independently reproduced foreign-DB race.
- [Current SQLite revision 3 proposal](vm-sqlite/revision3/00-STATUS.md), [implementation report](vm-sqlite/revision3/REPORT-V3.md), and [runnable diff](vm-sqlite/revision3/atomic-initialize-v3.patch): **not applied to canonical source**. Patch SHA-256 `40339ffa0ef57f5d6152c289344c7fda0011c70c0ea40338861495c3fa170a89`.
- [Independent final technical review](vm-sqlite-review/final/REVIEW-final.md): no remaining blocker in the reviewed initialization scope. This is not source integration, merge, native qualification, or release approval. Original/revision-1 negative evidence and [revision-2 cleanup failure status](vm-sqlite/revision2/00-STATUS.md) remain available.
- [FM03/fullcaller static contract review](fm03-static-review/FM03-32bb0f9-independent-contract-review.txt): 17 files parsed/read/hashed, with nine current-source hashes additionally matched by the publisher. Historical kit observations do not establish current fullcaller, fixed130, source-closure, or r7/native qualification.

## Integration decision still required

The current proposal broadens initialization acceptance from nonexistent/zero-byte files to SQLite databases with **application_id=0, user_version=0, and no sqlite_master entries**. It cannot identify the origin of an otherwise empty database. The owning writer must explicitly review that policy expansion before product adoption. Nonempty foreign databases and nonzero identity are rejected in the scoped tests.

The proposal fixes atomic schema/metadata/identity bootstrap, verifies identity while holding the writer transaction, and handles acquired-connection setup failures. Database commit, WAL setup, and artifact-directory creation are not a single filesystem transaction. Preserve the independent review's remaining limitations.

Existing repository regression runs remain 22 PASS / 3 POSIX owner-lock errors, rather than 25 PASS. Before/after reruns and concurrency repetitions must not be summed as unique qualification coverage. No native/provider execution, operating database mutation, migration, index deployment, merge, tag, or release was performed.

## Reproduction and evidence organization

Use a disposable exact-commit source checkout and a separate locked Python environment. Commands, working-directory aliases, actual exits, and recorded or explicitly unavailable timings are in each package's execution metadata. Restore the documented workspace names (`vm-portable`, `vm-evidence`, `vm-sqlite`, and `vm-sqlite-review`) or adjust artifact locations without changing the source candidate.

For a proposal, apply only the selected inert diff to a disposable copy and set `VM_SOURCE` to that copy. Copy the independent final review's Python files into the documented `vm-sqlite-review` location when using its recorded commands. Revision-3 author runners are self-contained in their folder with an `artifacts` scratch directory. Test fixtures create and remove synthetic databases; do not substitute operational database paths.

Each package carries hashes for its reviewed files. Manifests with `relative_path` use that field relative to their own directory. Cloud paths in logs were normalized to documented aliases; unnormalized originals were retained outside this branch. No source archive, wheel binary, whole AGENTS file, native transcript, runtime credential/profile/pin, operational DB/WAL, or personal user data is included.
