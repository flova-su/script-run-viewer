---
name: script-run-viewer
description: Turn a trusted local script or CLI command into a local browser UI with input fields, live logs, downloadable outputs, and replayable run records. Use for existing Python, Node, R, shell, research, or batch scripts when the user wants to run them from a browser.
---

# Local Script Run Viewer

Wrap the user-selected script with a browser interface. Keep the script's normal CLI, dependencies, and working directory.

1. Read the entrypoint or its documentation. Identify the command that already works, each input's type and default, and how the script writes results. Inspect source before any execution and use only scripts and fixtures the user has authorized you to run.
2. Read [the runner manifest](references/manifest.md). Create a viewer with `python3 scripts/scaffold.py <new-directory>` from this skill directory. The generated demo is a runnable example. Adapt `viewer.json` to reference the real command and replace demo fields and help copy. Keep the lightweight runner when the command fits the manifest.
3. Pass inputs as individual arguments. Use the run's output directory for results. When a script writes to fixed paths or needs positional adaptation, add a narrow wrapper beside the viewer. Keep the original entrypoint intact and preserve expected relative paths. Record the real entrypoint and wrapper in `code_files`.
4. Verify at least one representative run against the script's normal CLI behavior. Check that the UI shows the exit status and logs, that expected files can be downloaded, and that replay uses the saved inputs and actual parameters. If you cannot execute the selected script, label verification accordingly.
5. Start the viewer on loopback if the session permits it. Return its URL, generated files, and any assumptions about input or output formats. Give concise startup instructions if it could not be started.

The runner executes the fixed manifest command with local user privileges. Its template expects trusted code, serves one loopback user, and copies uploaded inputs into the run folder. Keep shareable examples synthetic. A request for a multi-user or internet-facing deployment needs a separate architecture with authentication and isolated workers.
