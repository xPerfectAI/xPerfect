import pytest

from workers_projects_runtime.profile_runtime import _redact_text as redact_output
from workers_projects_runtime.run_evidence import _redact_text as redact_evidence
from workers_projects_runtime.failure_classification import _redact_failure_text as redact_failure
from workers_projects_runtime.conversation_provider import StreamingRedactor


def redact_stream(value):
    redactor = StreamingRedactor(overlap=16)
    return ''.join(redactor.feed(value[i:i + 7]) for i in range(0, len(value), 7)) + redactor.flush()


REDACTORS = [redact_output, redact_evidence, redact_failure, redact_stream]


@pytest.mark.parametrize('redact', REDACTORS)
@pytest.mark.parametrize('value', [
    '[Earlier conversation](http://localhost:7190/c/56d7416e-4e31-5521-ac2f-d701e8102dba)',
    'https://chat.example.test/app/c/56d7416e-4e31-5521-ac2f-d701e8102dba',
    '"sourceDigest":"' + 'abcdef0123456789' * 4 + '"',
    '"artifactIdentifier":"accepted-source-98765432101234567890"',
])
def test_preserves_non_secret_links_and_identifiers(redact, value):
    assert redact(value) == value


@pytest.mark.parametrize('redact', REDACTORS)
@pytest.mark.parametrize('value, forbidden', [
    ('https://synthetic-user:synthetic-password@api.example.test/path', 'synthetic-password'),
    ('https://u:p@api.example.test/path', 'u:p@'),
    ('redis://:synthetic-password@cache.example.test:6379', 'synthetic-password'),
    ('123456789:' + 'synthetic_bot_token_value_123456789012345', 'synthetic_bot_token_value'),
    ('https://api.telegram.org/bot123456789:' + 'synthetic_bot_token_value_123456789012345' + '/sendMessage', 'synthetic_bot_token_value'),
    ('api_key=synthetic_api_credential_value', 'synthetic_api_credential_value'),
    ('Bearer synthetic_bearer_credential_value', 'synthetic_bearer_credential_value'),
])
def test_retains_credential_protection_in_output_evidence_failure_and_stream(redact, value, forbidden):
    assert forbidden not in redact(value)


@pytest.mark.parametrize('redact', REDACTORS)
@pytest.mark.parametrize('value', [
    'AK' + 'IA' + 'SYNTHETICKEY0000',
    'gh' + 'p_' + 'synthetic_credential_12345',
    'xo' + 'xb-' + 'synthetic-credential-12345',
    'ey' + 'Jheader.payload.signature',
    '-----BEGIN PRIVATE KEY-----\nsynthetic-private-key-bytes\n-----END PRIVATE KEY-----',
    '-----BEGIN PRIVATE KEY-----\nsynthetic-private-key-bytes',
])
def test_unlabelled_known_credentials_are_redacted_across_failure_and_output(redact, value):
    result = redact(value)
    assert value not in result
    assert 'synthetic-private-key-bytes' not in result
    assert 'REDACTED' in result


@pytest.mark.parametrize('redact', REDACTORS)
@pytest.mark.parametrize('identifier, secret', [
    ('AK' + 'IA' + 'A' * 16, 'synthetic_key_secret_' + 'a' * 20),
    ('A' + 'C' + '1' * 32, 'b' * 32),
    ('clientid' + '12345678', 'synthetic_client_secret_' + 'c' * 20),
])
def test_generic_credential_pairs_redact_both_halves(redact, identifier, secret):
    value = identifier + ':' + secret
    result = redact(value)
    assert identifier not in result
    assert secret not in result
    assert 'REDACTED' in result


@pytest.mark.parametrize('redact', REDACTORS)
def test_uri_authority_exclusion_still_redacts_credentials_in_its_path(redact):
    secret = 'synthetic_path_secret_' + 'd' * 24
    value = 'http://localhost:7190/path/clientid12345678:' + secret
    result = redact(value)
    assert result.startswith('http://localhost:7190/path/')
    assert secret not in result
    assert 'REDACTED' in result
