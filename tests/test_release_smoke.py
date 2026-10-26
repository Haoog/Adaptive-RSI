import io
import os
import unittest
import urllib.error
import urllib.request
import urllib.response
from unittest.mock import patch

from scripts import release_smoke


class MetadataTransport(urllib.request.HTTPSHandler):
    def __init__(self, status=200, body=b'{"tag_name":"v0.3.0"}'):
        super().__init__()
        self.requests = []
        self.status, self.body = status, body

    def https_open(self, request):
        self.requests.append(request)
        headers = {"Location": "https://release-assets.githubusercontent.com/elsewhere"} if self.status in [301, 302, 303, 307, 308] else {}
        response = urllib.response.addinfourl(io.BytesIO(self.body), headers, request.full_url, self.status)
        response.msg = "fixture"
        return response


class ReleaseMetadataTest(unittest.TestCase):
    def read_with(self, transport, token=None):
        opener = urllib.request.build_opener(release_smoke.NoMetadataRedirect(), transport)
        with patch.dict(os.environ, {"GH_TOKEN": token} if token else {}, clear=True), patch.object(release_smoke.urllib.request, "build_opener", return_value=opener):
            return release_smoke.latest_release()

    def test_optional_token_is_attached_only_to_fixed_metadata_request(self):
        for token in [None, "isolated-test-token"]:
            with self.subTest(authenticated=bool(token)):
                transport = MetadataTransport()
                self.assertEqual(self.read_with(transport, token), {"tag_name": "v0.3.0"})
                self.assertEqual(len(transport.requests), 1)
                request = transport.requests[0]
                self.assertEqual(request.full_url, release_smoke.RELEASE_API)
                self.assertEqual(request.get_header("Authorization"), "Bearer " + token if token else None)

    def test_redirect_never_forwards_token_or_masquerades_as_no_release(self):
        for code in [301, 302, 303, 307, 308]:
            transport = MetadataTransport(code)
            with self.subTest(code=code), self.assertRaises(urllib.error.HTTPError) as error:
                self.read_with(transport, "isolated-test-token")
            self.assertEqual(error.exception.code, code)
            self.assertEqual(len(transport.requests), 1)
            self.assertEqual(transport.requests[0].full_url, release_smoke.RELEASE_API)

    def test_asset_downloader_does_not_use_metadata_credentials(self):
        response = io.BytesIO(b"asset")
        response.url = "https://release-assets.githubusercontent.com/fixture.whl"
        with patch.dict(os.environ, {"GH_TOKEN": "isolated-test-token"}), patch("urllib.request.urlopen", return_value=response) as open_url:
            self.assertEqual(release_smoke.download("https://github.com/Haoog/Adaptive-RSI/releases/download/v0.3.0/fixture.whl", 1024), b"asset")
        request = open_url.call_args.args[0]
        self.assertIsNone(request.get_header("Authorization"))

    def test_only_not_found_means_no_prior_release(self):
        self.assertEqual(self.read_with(MetadataTransport(404)), {})
        for code in [401, 403, 429, 500]:
            with self.subTest(code=code), self.assertRaises(urllib.error.HTTPError) as error:
                self.read_with(MetadataTransport(code), "isolated-test-token")
            self.assertEqual(error.exception.code, code)

    def test_transient_metadata_failures_retry_without_relaxing_source(self):
        from unittest.mock import Mock
        success = io.BytesIO(b'{"tag_name":"v0.3.0"}')
        opener = Mock()
        opener.open.side_effect = [
            urllib.error.URLError("fixture connection failure"),
            urllib.error.HTTPError(release_smoke.RELEASE_API, 503, "fixture", {}, None),
            success,
        ]
        with patch.object(release_smoke.urllib.request, "build_opener", return_value=opener), patch.object(release_smoke.time, "sleep") as delay:
            self.assertEqual(release_smoke.latest_release(), {"tag_name": "v0.3.0"})
        self.assertEqual(opener.open.call_count, 3)
        self.assertEqual(delay.call_count, 2)
        self.assertTrue(all(call.args[0].full_url == release_smoke.RELEASE_API for call in opener.open.call_args_list))

    def test_oversized_or_invalid_metadata_fails(self):
        for body in [b"x" * (2 * 1024 * 1024 + 1), b"invalid-json"]:
            with self.assertRaises(ValueError):
                self.read_with(MetadataTransport(body=body))


if __name__ == "__main__":
    unittest.main()
