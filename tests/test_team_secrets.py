"""Machine reads preserve bytes and bind decrypt to the authorized name."""
import base64
import json
from src.dapier.dataops_secrets import SecretReader


def test_reader_binds_context_and_preserves_provider_json():
    calls = {}
    class Ddb:
        def get_item(self, **kwargs):
            calls['ddb'] = kwargs
            return {'Item': {'data': {'S': base64.b64encode(b'cipher').decode()}}}
    class Kms:
        def decrypt(self, **kwargs):
            calls['kms'] = kwargs
            return {'Plaintext': json.dumps({'value': '{ "password": "example" }\n'}).encode()}
    reader = SecretReader(table_name='store', key_id='key', region='eu-west-1', dynamodb=Ddb(), kms=Kms())
    assert reader.read('dapier.admin')['value'] == '{ "password": "example" }\n'
    assert calls['kms'] == {'KeyId': 'key', 'CiphertextBlob': b'cipher', 'EncryptionContext': {'secret': 'dapier.admin'}}
    assert calls['ddb']['Key']['SK']['S'] == 'SECRET#dapier.admin'


def test_template_has_no_sm_read_permissions_and_retains_originals():
    from pathlib import Path
    text = Path('template.yaml').read_text()
    assert 'secretsmanager:GetSecretValue' not in text
    assert 'AWSSecretsManagerGetSecretValuePolicy' not in text
    assert text.count('AWS::SecretsManager::Secret') == 2
    for name in ['YouTubeWebhookSecret', 'DapierAdminSecret']:
        block = text.split('  '+name+':', 1)[1].split('  WorkerFunction:', 1)[0]
        assert 'DeletionPolicy: Retain' in block
    assert 'kms:EncryptionContext:secret' in text
