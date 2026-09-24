# xPerfect OpenClaw reviewed runtime lock

This npm lock freezes the dependency graph selected for OpenClaw `2026.7.1-2`. The root package is
an exact integrity-pinned tarball. Exact root overrides keep the reviewed graph on patched releases
of `@hono/node-server`, `@modelcontextprotocol/sdk`, `brace-expansion`, `fast-uri`, and `tar`.

Generate and verify with the exact supported runtime used for this review:

```sh
npm ci --omit=dev
node_modules/.bin/openclaw --version
npm audit --omit=dev
```

Expected Node is `22.23.1`, npm is `10.9.8`, and OpenClaw reports `2026.7.1-2 (0790d9f)`. The
2026-07-30 production audit reports 0 critical, 0 high, and 1 moderate vulnerable package. The
remaining report is `protobufjs` inside the shrinkwrapped `@openclaw/ai` package; npm root
overrides cannot replace that nested lock. GHSA-j3f2-48v5-ccww requires parsing
attacker-influenced `.proto` schema text, which this runtime does not expose as an input surface.
Replace it when OpenClaw publishes a reviewed lock with `protobufjs >= 7.6.5`. This lock does not
authorize an xPerfect runtime entrypoint to fall back to another version. The owning runtime
contract is documented in `../../README.md`.
