# Version sweep

Required on **every Operator version increment**, including patch releases.
This checklist does not authorize publishing a package, public mirror, or demo.

1. Check the current checkout, merged work, and deployment scope. Record the old
   and new product versions. `operator_view.py::OP_VERSION` is the UI source of
   truth; the Codex transport also advertises the product version in `clientInfo`.
2. Search tracked Operator files for the old version and all version declarations,
   including README/current-release headings and ladder, templates (header,
   launchpad, About, demo), runtime metadata, manifests, build/deploy tools, and
   test expectations. Check Operator references in host scripts and adjacent
   deployment code too. Classify matches; do not replace them indiscriminately.
3. Update all current product-version surfaces together. Keep historical changelog
   entries, compatibility notes, dependency pins, protocol dates, schema/storage
   versions, and independently versioned components intact. For example,
   `operator-control`'s MCP server version is not the cockpit's product version,
   and neither is the delegation server's `operator_mcp.MCP_VERSION`; bump
   that one when the delegation tools or their contract change.
   Asset `rev` values are cache keys: bump references for changed assets, not
   merely to resemble the product version.
4. Check the generated demo template still uses `OP_VERSION`. If template changes
   require regeneration, follow `DEPLOY_DEMO.md` and inspect the generated diff.
   If packaging or public publishing is explicitly in scope, inspect those
   artifacts' version sources and release/tag metadata too. Otherwise report them
   as unchanged; this private service currently has no Operator package manifest.
5. Run the offline checks from `modules/operator`, using the project's test Python:

   ```sh
   python -m pytest tests/test_operator_view.py::test_release_version_surfaces_stay_in_sync tests/test_codex_transport.py::test_client_version_matches_operator_release -q
   ```

   Also run affected rendering/packaging tests. Repeat the old-version search and
   explain surviving matches. No real model turn is needed.
6. When deployment is requested, use the guarded host-app release workflow.
   Verify the served header/launchpad/About version and changed asset references
   from the intended device-facing endpoint. Report the release commit, version,
   checks run, and what was not deployed or published. Source/test checks alone
   do not prove the live service was updated.
