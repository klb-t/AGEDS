import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location('verify_repository', ROOT / 'scripts/verify_repository.py')
verify = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(verify)


def _phones(text):
    return [m.group(1) for m in verify.PHONE.finditer(text) if not verify._fictional_phone(m.group(1))]


def test_private_looking_values_are_flagged_and_fictional_ones_allowed():
    assert _phones('tel +48 601 284 597') and _phones('call 601 284 597') and _phones('06-87654321')
    assert _phones('x 0031 6 2847 1934')
    for fictional in ('+31 6 0000 1234', '+48 000 000 123', '+48123456789', '+1 202 555 0143', '0.00666666030884'):
        assert not _phones(fictional), fictional
    assert not verify._email_allowed('gmail.com')
    assert verify._email_allowed('example.org') and verify._email_allowed('mail.example.com')
    assert verify.USER_PATH.search('/home/someone/Documents/') and verify.USER_PATH.search('/Users/someone/x')
    assert verify.USER_PATH.search('C:\\Users\\someone\\x')
    assert not verify.USER_PATH.search('/home/user/AGEDS/') and not verify.USER_PATH.search('/root/x')


def test_current_tree_has_no_privacy_findings():
    scanned, findings = verify.privacy_findings()
    assert scanned > 0
    assert findings == []
