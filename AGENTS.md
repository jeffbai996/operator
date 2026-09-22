# Operator

Read `CLAUDE.md` before changing the browser feed; its streamer safety rules apply
to every agent working here.

## Version increments are release gates

Every Operator version increment must include a full version-number sweep in the
same change. Follow `RELEASING.md` before calling the bump complete: check runtime
metadata, every displayed version, current documentation, applicable packaging,
generated/demo surfaces, and version assertions. Updating `OP_VERSION` alone is
not sufficient.

Preserve historical release numbers and separately versioned dependencies,
protocols, schemas, and asset-cache revisions. Never blanket-replace a version.
Run the offline version checks, verify the served version when deployment is in
scope, and explicitly report any public mirror, package, or demo left unchanged.
Do not spend model quota on a version sweep.
