"""Published browser distributions and licenses remain byte-exact without npm."""
from hashlib import sha256
from pathlib import Path
import tomllib


FRONTEND = Path(__file__).parents[1]
STATIC = FRONTEND / 'src' / 'glass_drive_ui' / 'static'
VENDOR = STATIC / 'vendor'

# Exact members from integrity-verified official npm tarballs; see README.
UPSTREAM_HASHES = {
    'markdown-it-15.0.3.mjs': 'd5432491790141da046b1197353705db03264f40b5727f3b4b591f5aba193e30',
    'markdown-it-LICENSE': '792c48c5a849a15fdf9e37e8bcf9e6d1dd13b32b46c642a748a0a46a9919d473',
    'mdurl-LICENSE': '6b6a356c4337e2446841f30f3cdf7e7023af8c5260b5e347c0f987b9f6aa2644',
    'entities-LICENSE': 'cb992345949ccd6e8394b2cd6c465f7b897c864f845937dbf64e8997f389e164',
    'oxc-project-runtime-LICENSE': '95ced5ecf1133fbf41d409b5555c86c344f83f3b019926057ddbc07cfdcc27b3',
    'rolldown-LICENSE': '23ecfff35a5a2e80d92142f75228912c3b1abc4b5a8337a821ff4397e2f9f734',
    'xterm-5.5.0/xterm.js': '1f991ac3b4b283ebf96e60ae23a00a52765dd3a2e46fa6fdda9f1aab032f7495',
    'xterm-5.5.0/xterm.css': 'ba8e6985669488981ccf40c0cefe3aba80722cb6c92de7ad628b0bd717faf2b6',
    'xterm-5.5.0/LICENSE': 'b569f629d00f2626a8100df2a1798210535621e42164dfd426a6fe5aac7b0ccd',
}


def test_browser_vendor_distributions_and_licenses_match_official_release_bytes():
    for relative, expected in UPSTREAM_HASHES.items():
        assert sha256((VENDOR / relative).read_bytes()).hexdigest() == expected, relative


def test_both_markdown_consumers_use_the_same_patched_browser_release():
    for consumer in ('conversation.js', 'result-markdown.js'):
        source = (STATIC / consumer).read_text()
        assert "from './vendor/markdown-it-15.0.3.mjs'" in source, consumer
        assert 'markdown-it-15.0.2' not in source, consumer
    assert not (VENDOR / 'markdown-it-15.0.2.mjs').exists()


def test_frontend_ca_bundle_lock_includes_the_emsign_root_removal():
    lock = tomllib.loads((FRONTEND / 'uv.lock').read_text())
    certifi = next(package for package in lock['package'] if package['name'] == 'certifi')
    assert tuple(map(int, certifi['version'].split('.'))) >= (2026, 10, 19)
