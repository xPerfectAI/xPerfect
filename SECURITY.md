# Security

Please report suspected vulnerabilities through
[GitHub private vulnerability reporting](https://github.com/xPerfectAI/xPerfect/security/advisories/new).
Do not put exploit details in public issues, pull requests or comments.

Include the affected revision, deployment mode, expected
and observed behavior, and a minimal reproduction using synthetic data. Remove credentials,
tokens, signed links, customer information and unrelated logs. Test only systems you own or have
permission to assess.

Include your installed version even if it is old. No supported-version window, response deadline
or bounty is promised here. Ordinary bugs without a security impact can use
[GitHub Issues](https://github.com/xPerfectAI/xPerfect/issues).

## Desktop runtime boundaries

The legacy `DockerSandbox` workstation runs the worker and its browser in the same container.
When Chromium user namespaces are enabled (the default), its
`WPR_SANDBOX_CHROMIUM_USERNS_SECURITY_OPT` setting defaults to `seccomp=unconfined` for that
whole container. An explicit operator setting can change it. This legacy route does not have
the managed display sidecar's isolation; do not treat it as Docker's default seccomp policy.

Managed `WorkspaceBox` workers use a separate display container. Its pinned browser seccomp
profile applies to that display container; the AI box keeps its own Docker policy. This does
not isolate the human's desktop from the worker: anything opened or signed in there is shared
with that worker. Check the actual runtime and applied policy when assessing a deployment.
