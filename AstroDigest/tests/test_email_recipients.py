"""Multiple recipients survive settings saves and become separate SMTP targets."""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch
from email.parser import Parser

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src import gui, notifier
from src.email_addresses import parse_recipients


class RecipientParsingTests(unittest.TestCase):
    def test_common_separators_and_duplicates(self):
        self.assertEqual(parse_recipients('one@example.com； two+tag@example.org,one@example.com\nthree@example.net'),
                         ['one@example.com', 'two+tag@example.org', 'three@example.net'])

    def test_invalid_address_is_not_silently_dropped(self):
        for value in ['valid@example.com, invalid', 'a@example.com\r\nBcc: victim@example.org', 'a@@example.com']:
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_recipients(value)


class RecipientSettingsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / '.env').write_text('EMAIL_APP_PASSWORD="saved-test-password"\n')
        for mock in [patch.object(gui, '_PROJECT_DIR', self.root),
                     patch.object(gui, '_needs_setup', return_value=False),
                     patch.object(gui, 'load_preferences', return_value={}),
                     patch.object(gui, 'save_preferences'),
                     patch.object(gui, '_apply_interests'),
                     patch.object(gui, '_refresh_email_environment'),
                     patch.dict(os.environ, {}, clear=True)]:
            mock.start()
            self.addCleanup(mock.stop)
        self.client = gui.app.test_client()
        self.form = dict(provider='deepseek', api_key='test-key', model='test-model',
                         enable_email='on', email_address='sender@example.com',
                         email_recipients='one@example.com; two@example.org，one@example.com',
                         smtp_server='smtp.example.com', smtp_protocol='ssl', smtp_port='465',
                         email_password='', settings_section='email')

    def test_save_reload_and_repeated_save_keep_all_recipients(self):
        for route in ['/settings/save', '/setup', '/settings/save']:
            with self.subTest(route=route):
                self.assertEqual(self.client.post(route, data=self.form).status_code, 302)
                config, env = gui._load_config_and_env()
                self.assertEqual(config['email']['recipient'], 'one@example.com, two@example.org')
                self.assertEqual(env['EMAIL_RECIPIENT'], config['email']['recipient'])
                self.assertEqual(env['EMAIL_SENDER'], 'sender@example.com')
                self.assertEqual(env['EMAIL_APP_PASSWORD'], 'saved-test-password')
                page = self.client.get('/settings').get_data(as_text=True)
                self.assertIn('value="one@example.com, two@example.org"', page)

    def test_learned_weights_have_independent_form_ownership(self):
        page = self.client.get('/settings').get_data(as_text=True)
        self.assertIn('<form id="learned-profile-form" onsubmit="return false">', page)
        self.assertIn('form="learned-profile-form" step="any"', page)
        self.assertIn('if (field.form !== form) return;', page)
        self.assertNotIn('<button class="lp-btn', page)

    def test_blank_recipients_default_to_sender(self):
        self.form['email_recipients'] = ''
        self.assertEqual(self.client.post('/settings/save', data=self.form).status_code, 302)
        self.assertEqual(gui._load_config_and_env()[0]['email']['recipient'], 'sender@example.com')

    def test_older_client_preserves_previously_saved_recipients(self):
        self.client.post('/settings/save', data=self.form)
        del self.form['email_recipients']
        self.client.post('/settings/save', data=self.form)
        self.assertEqual(gui._load_config_and_env()[0]['email']['recipient'], 'one@example.com, two@example.org')

    def test_invalid_submission_leaves_saved_files_untouched(self):
        self.client.post('/settings/save', data=self.form)
        before = {p.name:p.read_bytes() for p in self.root.iterdir() if p.is_file()}
        for field, value in [('email_recipients','one@example.com, bad-address'),
                             ('email_address','one@example.com, two@example.com')]:
            with self.subTest(field=field):
                form = dict(self.form, **{field:value})
                response = self.client.post('/settings/save', data=form)
                self.assertEqual(response.status_code, 400)
                self.assertEqual(before, {p.name:p.read_bytes() for p in self.root.iterdir() if p.is_file()})


class RecipientDeliveryTests(unittest.TestCase):
    def test_every_recipient_is_in_smtp_envelope_and_to_header(self):
        for use_ssl in [True, False]:
            with self.subTest(use_ssl=use_ssl):
                server = MagicMock()
                server.__enter__.return_value = server
                server.sendmail.return_value = {}
                with patch.dict(os.environ, {'EMAIL_APP_PASSWORD':'test-password'}, clear=True), \
                     patch.object(notifier.smtplib, 'SMTP_SSL', return_value=server), \
                     patch.object(notifier.smtplib, 'SMTP', return_value=server):
                    ok = notifier.send_email('Test', 'Body', {
                        'enabled':True, 'sender':'sender@example.com',
                        'recipient':'one@example.com; two@example.org',
                        'smtp_server':'smtp.example.com', 'use_ssl':use_ssl,
                    })
                self.assertTrue(ok)
                server.login.assert_called_once_with('sender@example.com','test-password')
                sender, recipients, raw = server.sendmail.call_args.args
                self.assertEqual(sender, 'sender@example.com')
                self.assertEqual(recipients, ['one@example.com','two@example.org'])
                self.assertEqual(Parser().parsestr(raw)['To'], 'one@example.com, two@example.org')

    def test_partial_refusal_is_not_reported_as_success(self):
        for use_ssl in [True, False]:
            with self.subTest(use_ssl=use_ssl):
                server = MagicMock()
                server.__enter__.return_value = server
                server.sendmail.return_value = {'two@example.org': (550, b'No such user')}
                with patch.dict(os.environ, {'EMAIL_APP_PASSWORD': 'test'}, clear=True), \
                     patch.object(notifier.smtplib, 'SMTP_SSL', return_value=server), \
                     patch.object(notifier.smtplib, 'SMTP', return_value=server), \
                     patch('builtins.print') as output:
                    self.assertFalse(notifier.send_email('Test', 'Body', {
                        'enabled': True, 'sender': 'sender@example.com',
                        'recipient': 'one@example.com, two@example.org', 'use_ssl': use_ssl,
                    }))
                self.assertIn('rejected recipients: two@example.org', output.call_args.args[0])
                server.sendmail.assert_called_once()  # no duplicate automatic resend

    def test_partial_daily_delivery_does_not_mark_date_sent(self):
        server = MagicMock()
        server.__enter__.return_value = server
        server.sendmail.return_value = {'two@example.org': (550, b'Rejected')}
        with tempfile.TemporaryDirectory() as tmp, \
             patch.dict(os.environ, {'EMAIL_APP_PASSWORD': 'test'}, clear=True), \
             patch.object(notifier.smtplib, 'SMTP_SSL', return_value=server):
            state = Path(tmp) / 'email-state.json'
            self.assertFalse(notifier.send_digest_notification([], {
                'enabled': True, 'sender': 'sender@example.com',
                'recipient': 'one@example.com, two@example.org',
            }, '2026-09-09', state_path=state))
            self.assertFalse(state.exists())

    def test_bad_recipient_aborts_before_any_smtp_connection(self):
        with patch.dict(os.environ, {'EMAIL_APP_PASSWORD':'test-password'}, clear=True), \
             patch.object(notifier.smtplib, 'SMTP_SSL') as smtp:
            ok = notifier.send_email('Test', 'Body', {
                'enabled':True, 'sender':'sender@example.com', 'recipient':'one@example.com, invalid',
            })
        self.assertFalse(ok)
        smtp.assert_not_called()


if __name__ == '__main__':
    unittest.main()
