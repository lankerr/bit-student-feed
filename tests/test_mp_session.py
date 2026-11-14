import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from bitfeed.mp_session import MpError, MpSession


class MpSessionTest(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.path=Path(self.tmp.name)/'session.json'
        self.path.write_text(json.dumps({'token':'test-only','cookies':[]}))
        self.session=MpSession(self.path,http=Mock())

    def tearDown(self):
        self.tmp.cleanup()

    def test_home_layout_change_is_verified_with_the_operators_account_list(self):
        responses=[Mock(text='<html>new layout</html>'),Mock()]
        responses[1].json.return_value={'base_resp':{'ret':0},'biz_list':{'list':[{'name':'own-account'}]}}
        with patch.object(self.session,'_get',side_effect=responses):
            self.session.keepalive()
        self.assertIn('kept_alive_at',json.loads(self.path.read_text()))

    def test_ambiguous_response_is_blocked_without_demanding_a_new_login(self):
        responses=[Mock(text='<html>verification</html>'),Mock()]
        responses[1].json.return_value={'base_resp':{'ret':200013}}
        with patch.object(self.session,'_get',side_effect=responses):
            with self.assertRaises(MpError) as raised:self.session.keepalive()
        self.assertEqual(raised.exception.state,'blocked')

    def test_explicit_revocation_still_needs_reauthorization(self):
        with patch.object(self.session,'_get',return_value=Mock(text='请重新登录')):
            with self.assertRaises(MpError) as raised:self.session.keepalive()
        self.assertEqual(raised.exception.state,'auth_expired')

    def test_unreadable_session_is_not_misreported_as_needing_a_new_qr(self):
        with patch.object(Path,'read_text',side_effect=PermissionError('private session')):
            with self.assertRaises(PermissionError):MpSession(self.path,http=Mock())
