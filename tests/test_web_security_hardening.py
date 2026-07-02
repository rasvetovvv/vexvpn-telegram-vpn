import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from fastapi import HTTPException
from fastapi.testclient import TestClient

from bot.web import auth as web_auth
from bot.web import main as web_main
from bot.web import rate_limit as rate_limit_mod
from bot.web.rate_limit import RateLimit, _BUCKETS


class FakeRequest:
    def __init__(self, peer: str, headers: dict[str, str] | None = None):
        self.client = SimpleNamespace(host=peer)
        self.headers = headers or {}


class WebSecurityHardeningTests(unittest.TestCase):
    def tearDown(self):
        _BUCKETS.clear()

    def test_init_data_requires_auth_date_even_with_valid_hash(self):
        pairs = {'user': '{"id":123}'}
        data_check = '\n'.join(f'{k}={v}' for k, v in sorted(pairs.items()))
        sig = web_auth.hmac.new(web_auth._secret_key(), data_check.encode(), web_auth.hashlib.sha256).hexdigest()
        init_data = f'user={pairs["user"]}&hash={sig}'
        with self.assertRaises(HTTPException) as ctx:
            web_auth.validate_init_data(init_data, max_age=3600)
        self.assertEqual(ctx.exception.status_code, 401)
        self.assertIn('auth_date', ctx.exception.detail)

    def test_init_data_expired_window_rejected(self):
        old = int(time.time()) - 7200
        pairs = {'auth_date': str(old), 'user': '{"id":123}'}
        data_check = '\n'.join(f'{k}={v}' for k, v in sorted(pairs.items()))
        sig = web_auth.hmac.new(web_auth._secret_key(), data_check.encode(), web_auth.hashlib.sha256).hexdigest()
        init_data = f'auth_date={old}&user={pairs["user"]}&hash={sig}'
        with self.assertRaises(HTTPException) as ctx:
            web_auth.validate_init_data(init_data, max_age=3600)
        self.assertEqual(ctx.exception.status_code, 401)
        self.assertIn('expired', ctx.exception.detail)

    def test_client_ip_ignores_spoofed_headers_from_untrusted_peer(self):
        req = FakeRequest('203.0.113.10', {'x-forwarded-for': '198.51.100.99', 'x-real-ip': '198.51.100.100'})
        with patch.object(web_main.settings, 'trusted_proxy_ips', ''):
            self.assertEqual(web_main._client_ip(req), '203.0.113.10')

    def test_client_ip_uses_xff_only_from_trusted_proxy(self):
        req = FakeRequest('172.23.0.3', {'x-forwarded-for': '198.51.100.99, 172.23.0.3'})
        with patch.object(web_main.settings, 'trusted_proxy_ips', '172.23.0.0/16'):
            self.assertEqual(web_main._client_ip(req), '198.51.100.99')

    def test_public_rate_limit_uses_client_ip_before_upstream_work(self):
        req = FakeRequest('203.0.113.20')
        rule = RateLimit(limit=2, window_seconds=60)
        with patch.object(rate_limit_mod, '_redis_client', return_value=None):
            web_main._require_public_ip_rate_limit(req, 'unit-test-public', rule)
            web_main._require_public_ip_rate_limit(req, 'unit-test-public', rule)
            with self.assertRaises(HTTPException) as ctx:
                web_main._require_public_ip_rate_limit(req, 'unit-test-public', rule)
        self.assertEqual(ctx.exception.status_code, 429)
        # The stored key is namespaced and hashed; raw IP must not appear in buckets.
        self.assertFalse(any('203.0.113.20' in key for key in _BUCKETS))

    def test_marzban_tls_verify_defaults_to_enabled(self):
        with patch.object(web_main.settings, 'marzban_tls_ca_file', ''):
            self.assertIs(web_main._marzban_tls_verify(), True)
        with patch.object(web_main.settings, 'marzban_tls_ca_file', '/etc/ssl/marzban-ca.pem'):
            self.assertEqual(web_main._marzban_tls_verify(), '/etc/ssl/marzban-ca.pem')

    def test_hsts_header_is_set(self):
        client = TestClient(web_main.app)
        response = client.get('/healthz')
        self.assertEqual(response.headers.get('strict-transport-security'), 'max-age=31536000; includeSubDomains')

    def test_redis_rate_limit_path_returns_429_when_shared_bucket_exceeds_limit(self):
        fake_pipe = Mock()
        fake_pipe.incr.return_value = fake_pipe
        fake_pipe.ttl.return_value = fake_pipe
        fake_pipe.execute.side_effect = [(1, -1), (2, 60)]
        fake_redis = Mock()
        fake_redis.pipeline.return_value = fake_pipe
        with patch.object(rate_limit_mod, '_redis_client', return_value=fake_redis):
            rate_limit_mod.require_rate_limit('redis-unit', RateLimit(limit=1, window_seconds=60))
            with self.assertRaises(HTTPException) as ctx:
                rate_limit_mod.require_rate_limit('redis-unit', RateLimit(limit=1, window_seconds=60))
        self.assertEqual(ctx.exception.status_code, 429)
        fake_redis.expire.assert_called_with('rl:60:redis-unit', 60)

    def test_own_subscription_url_requires_exact_scheme_host_and_port(self):
        with patch.object(web_main.settings, 'marzban_base_url', 'https://vpn.example.com:8002'):
            self.assertTrue(web_main._is_own_subscription_url('https://vpn.example.com:8002/sub/token'))
            self.assertFalse(web_main._is_own_subscription_url('https://vpn.example.com/sub/token'))
            self.assertFalse(web_main._is_own_subscription_url('https://vpn.example.com:8443/sub/token'))
            self.assertFalse(web_main._is_own_subscription_url('http://vpn.example.com:8002/sub/token'))
            self.assertFalse(web_main._is_own_subscription_url('https://evil.example.com:8002/sub/token'))
        with patch.object(web_main.settings, 'marzban_base_url', 'https://vpn.example.com'):
            self.assertTrue(web_main._is_own_subscription_url('https://vpn.example.com/sub/token'))
            self.assertTrue(web_main._is_own_subscription_url('https://vpn.example.com:443/sub/token'))
            self.assertFalse(web_main._is_own_subscription_url('https://vpn.example.com:444/sub/token'))


if __name__ == '__main__':
    unittest.main()
