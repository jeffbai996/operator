# Control test boundary

The control MCP includes job tools and the stdlib workspace store. Its standalone
runner stages control, vision, operator_job_tools and operator_workspace into a
temporary flat artifact. Optional file-transfer dependencies load only when that
tool is invoked. Duplicate filenames fail rather than overwrite another module.
The child test process clears inherited Python paths and job credentials and uses
an empty temporary home. The monorepo runner retains repository test admission.

The served browser harness blocks process launch by default. Saved-task tests
exercise the real HTTP origin guard and stored-bundle dispatcher with only the
consequential launch replaced for valid bundles. Invalid bots reach the real
runner rejection; they must never be accepted to display hostile status text.
Rejected runs remain outside the running state and do not update last_run. Markup checks cover both
rejection and error presentation.
