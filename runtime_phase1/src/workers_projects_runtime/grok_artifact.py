"""Immutable official Grok Build artifacts, reviewed as static download inputs.

Execution compatibility is a separate release gate. Binaries are fetched from
xAI's documented CLI distribution endpoint, never via a mutable installer.
"""
GROK_VERSION = '1.0.34'
GROK_LINUX_SHA256 = {
    'amd64': 'be5905e107d2b8b5f3c142d21ecfe4c8fd32a913d2fd551b788707930c4dc80d',
    'arm64': '39ab87666877d64ef3a40aa60fbe0c3b6a6acd7001b78fe60e2c76bb6cfc4a94',
}
GROK_ARTIFACT_PROVENANCE = GROK_VERSION + ':' + ':'.join(GROK_LINUX_SHA256[key] for key in sorted(GROK_LINUX_SHA256))


def docker_install_instruction():
    return (
        'RUN arch=$(dpkg --print-architecture) && case "$arch" in '
        f'amd64) platform=linux-x86_64; grok_sha={GROK_LINUX_SHA256["amd64"]} ;; '
        f'arm64) platform=linux-aarch64; grok_sha={GROK_LINUX_SHA256["arm64"]} ;; '
        '*) echo "Unsupported Grok Build architecture" >&2; exit 1 ;; esac '
        f'&& curl -fsSL "https://x.ai/cli/grok-{GROK_VERSION}-${{platform}}" -o /tmp/grok '
        '&& echo "$grok_sha  /tmp/grok" | sha256sum -c - '
        '&& install -m 0755 /tmp/grok /usr/local/bin/grok && rm /tmp/grok'
    )
