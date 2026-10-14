"""Optional model setup requires explicit consent and never stores credentials."""
import socket
import sys
import traceback
import unittest
from unittest.mock import patch

from PySide6.QtCore import QTimer, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog, QLineEdit

from test_qt import APP
from snipboard.model_search import ModelConfig
from snipboard.qt_model_settings import ModelSettingsDialog, configure_model


class ModelSettingsTests(unittest.TestCase):
    def setUp(self):
        self.dialogs = []
        self.callback_errors = []
        self.previous_hook = sys.excepthook
        sys.excepthook = lambda *args: self.callback_errors.append(
            ''.join(traceback.format_exception(*args)))

    def tearDown(self):
        for dialog in self.dialogs:
            dialog.reject()
            dialog.deleteLater()
        APP.processEvents()
        sys.excepthook = self.previous_hook
        self.assertEqual(self.callback_errors, [], '\n'.join(self.callback_errors))

    def dialog(self, existing=None):
        dialog = ModelSettingsDialog(existing=existing)
        self.dialogs.append(dialog)
        dialog.show()
        APP.processEvents()
        return dialog

    def fill(self, dialog, *, local=False):
        dialog.base_url_input.setText('http://127.0.0.1:1234/v1' if local else 'https://vision.example/v1')
        dialog.model_input.setText('drawing-vision')
        dialog.api_key_input.setText('' if local else 'test-secret-not-for-storage')

    def test_default_is_bounded_and_no_send_permission_is_implied(self):
        dialog = self.dialog()
        self.assertEqual(dialog.candidate_limit.value(), 12)
        self.assertEqual(dialog.batch_size.value(), 4)
        self.assertEqual(dialog.timeout.value(), 30)
        self.assertEqual(dialog.api_key_input.echoMode(), QLineEdit.Password)
        self.assertFalse(dialog.consent.isChecked())
        self.assertFalse(dialog.apply_button.isEnabled())
        self.fill(dialog)
        self.assertFalse(dialog.apply_button.isEnabled())
        dialog.accept()
        self.assertEqual(dialog.result(), QDialog.Rejected)
        self.assertIsNone(dialog.config)
        self.assertTrue(dialog.isVisible())
        self.assertIn('允许', dialog.error_label.text())

    def test_accept_returns_in_memory_config_and_clears_password_widget(self):
        dialog = self.dialog()
        self.fill(dialog)
        dialog.consent.setChecked(True)
        self.assertTrue(dialog.apply_button.isEnabled())
        with patch('snipboard.catalog.Catalog.set_setting', side_effect=AssertionError('Credential persistence')), \
                patch.object(socket, 'create_connection', side_effect=AssertionError('Unexpected connection')):
            QTest.mouseClick(dialog.apply_button, Qt.LeftButton)
        self.assertEqual(dialog.result(), QDialog.Accepted)
        self.assertEqual(dialog.config.base_url, 'https://vision.example/v1')
        self.assertEqual(dialog.config.model, 'drawing-vision')
        self.assertEqual(dialog.config.api_key, 'test-secret-not-for-storage')
        self.assertEqual(dialog.api_key_input.text(), '')
        self.assertNotIn('test-secret-not-for-storage', repr(dialog.config))

    def test_editing_existing_config_requires_new_explicit_consent(self):
        existing = ModelConfig('https://vision.example/v1', 'drawing-vision', 'existing-secret',
                               candidate_limit=20, batch_size=2, timeout=45)
        dialog = self.dialog(existing)
        self.assertEqual(dialog.base_url_input.text(), existing.base_url)
        self.assertEqual(dialog.api_key_input.text(), existing.api_key)
        self.assertEqual(dialog.candidate_limit.value(), 20)
        self.assertFalse(dialog.consent.isChecked())
        self.assertFalse(dialog.apply_button.isEnabled())
        dialog.reject()
        self.assertIsNone(dialog.config)
        self.assertEqual(dialog.api_key_input.text(), '')
        self.assertEqual(existing.api_key, 'existing-secret')

    def test_loopback_service_needs_consent_even_without_api_key(self):
        dialog = self.dialog()
        self.fill(dialog, local=True)
        self.assertFalse(dialog.apply_button.isEnabled())
        dialog.consent.setChecked(True)
        self.assertTrue(dialog.apply_button.isEnabled())
        dialog.accept()
        self.assertEqual(dialog.result(), QDialog.Accepted)
        self.assertEqual(dialog.config.api_key, '')
        self.assertEqual(dialog.config.base_url, 'http://127.0.0.1:1234/v1')

    def test_invalid_remote_http_stays_open_without_enabling_service(self):
        dialog = self.dialog()
        self.fill(dialog)
        dialog.base_url_input.setText('http://vision.example/v1')
        dialog.consent.setChecked(True)
        self.assertFalse(dialog.apply_button.isEnabled())
        self.assertTrue(dialog.error_label.text())
        dialog.accept()
        self.assertEqual(dialog.result(), QDialog.Rejected)
        self.assertIsNone(dialog.config)
        self.assertTrue(dialog.isVisible())

    def test_credential_url_is_rejected_without_showing_secrets(self):
        dialog = self.dialog()
        self.fill(dialog)
        dialog.base_url_input.setText('https://private-user:private-password@vision.example/v1')
        dialog.consent.setChecked(True)
        self.assertFalse(dialog.apply_button.isEnabled())
        self.assertTrue(dialog.error_label.text())
        for secret in ('private-user', 'private-password', 'test-secret-not-for-storage'):
            self.assertNotIn(secret, dialog.error_label.text())

    def test_budget_controls_cannot_exceed_image_and_wait_limits(self):
        dialog = self.dialog()
        self.fill(dialog)
        dialog.candidate_limit.setValue(1000)
        dialog.batch_size.setValue(1000)
        dialog.timeout.setValue(1000)
        dialog.consent.setChecked(True)
        dialog.accept()
        self.assertEqual(dialog.config.candidate_limit, 40)
        self.assertEqual(dialog.config.batch_size, 4)
        self.assertEqual(dialog.config.timeout, 60)

    def test_configure_wrapper_returns_config_only_after_real_acceptance(self):
        def accept_modal():
            dialog = QApplication.activeModalWidget()
            try:
                self.assertIsInstance(dialog, ModelSettingsDialog)
                self.fill(dialog, local=True)
                dialog.consent.setChecked(True)
                dialog.accept()
            finally:
                if dialog is not None and dialog.isVisible():
                    dialog.reject()

        QTimer.singleShot(0, accept_modal)
        with patch.object(socket, 'create_connection', side_effect=AssertionError('Unexpected connection')):
            config = configure_model(None)
        self.assertIsInstance(config, ModelConfig)
        self.assertEqual(config.model, 'drawing-vision')

    def test_configure_wrapper_returns_none_when_dismissed(self):
        def reject_modal():
            dialog = QApplication.activeModalWidget()
            if dialog is not None:
                self.fill(dialog)
                dialog.reject()

        QTimer.singleShot(0, reject_modal)
        self.assertIsNone(configure_model(None))


if __name__ == '__main__':
    unittest.main()
