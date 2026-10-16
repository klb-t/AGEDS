import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location('verify_repository', ROOT / 'scripts/verify_repository.py')
verify = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(verify)


def _phones(text):
    return [m.group(1) for m in verify.PHONE.finditer(text) if not verify._fictional_phone(m.group(1))]


def _join(*parts, sep=' '):
    # Built at runtime so this tracked file does not itself contain the samples.
    return sep.join(parts)


def test_private_looking_values_are_flagged_and_fictional_ones_allowed():
    local = _join('601', '284', '597')
    assert _phones('tel +48 ' + local) and _phones('call ' + local) and _phones(_join('06', '87654321', sep='-'))
    assert _phones('x ' + _join('0031', '6', '2847', '1934'))
    for fictional in ('+31 6 0000 1234', '+48 000 000 123', '+48123456789', '+1 202 555 0143', '0.00666666030884'):
        assert not _phones(fictional), fictional
    assert not verify._email_allowed('gmail.com')
    assert verify._email_allowed('example.org') and verify._email_allowed('mail.example.com')
    home = _join('', 'home', 'someone', 'Documents', '', sep='/')
    assert verify.USER_PATH.search(home) and verify.USER_PATH.search(_join('', 'Users', 'someone', 'x', sep='/'))
    assert verify.USER_PATH.search(_join('C:', 'Users', 'someone', 'x', sep='\\'))
    assert not verify.USER_PATH.search('/home/user/AGEDS/') and not verify.USER_PATH.search('/root/x')


def test_current_tree_has_no_privacy_findings():
    scanned, findings = verify.privacy_findings()
    assert scanned > 0
    assert findings == []
