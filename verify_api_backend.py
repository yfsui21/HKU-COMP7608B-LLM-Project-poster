"""Offline checks for the PosterForest HTTPS model backend and usage provenance."""
import io
import json
import os
import tempfile
from pathlib import Path
from unittest.mock import patch

from examples.forest_local.gpt_backend import complete


def main():
    payload = {"model": "test-model", "messages": [{"role": "user", "content": "Return a title."}]}
    response = {"model": "resolved-model", "choices": [{"message": {"content": '{"title":"Example"}'}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 4}}
    with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {
            "OPENAI_BASE_URL": "https://example.invalid/v1", "OPENAI_API_KEY": "local-test-secret"}):
        def request(req, timeout):
            assert req.full_url == "https://example.invalid/v1/chat/completions"
            assert req.get_header("Authorization") == "Bearer local-test-secret"
            assert json.loads(req.data) == payload
            return io.BytesIO(json.dumps(response).encode())
        with patch("urllib.request.urlopen", side_effect=request):
            result = complete(payload, "case", work=Path(tmp), backend="api")
        assert result["model"] == "resolved-model"
        assert result["usage"] == {"prompt_tokens": 10, "completion_tokens": 4, "reported": True}
        assert all("local-test-secret" not in p.read_text() for p in Path(tmp).rglob("*.txt"))
        response.pop("usage")
        with patch("urllib.request.urlopen", side_effect=request):
            result = complete(payload, "unknown-usage", work=Path(tmp), backend="api")
        assert result["usage"]["reported"] is False
        with patch.dict(os.environ, {"OPENAI_BASE_URL": "http://example.invalid/v1"}), \
                patch("urllib.request.urlopen") as network:
            try:
                complete(payload, "unsafe-url", work=Path(tmp), backend="api")
            except ValueError:
                pass
            else:
                raise AssertionError("Plain HTTP must not receive a credential")
            network.assert_not_called()
    print("PASS: HTTPS routing, returned model, unknown usage and credential exclusion")


if __name__ == "__main__":
    main()
