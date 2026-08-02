# Security policy

Use a private GitHub Security Advisory to report a suspected vulnerability. Do
not attach proprietary CAD, solver results, local executable paths, credentials
or customer data to a public issue.

This repository launches locally installed engineering programs. Treat every
tool path and configuration file as untrusted input: keep `shell=False`, use
argument lists, reject implicit Windows executables, and review command
manifests before sharing them. `config/tools.json`, private geometry and `runs/`
are intentionally excluded from version control.

No supported release line has been declared yet. Reports should identify the
commit, Python version, operating system, and the smallest safe reproducer.
