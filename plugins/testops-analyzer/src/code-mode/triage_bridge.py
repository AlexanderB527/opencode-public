"""Run the existing triage collector without publishing canonical artifacts."""
import io
import json
import os
import re
import sys
import urllib.error
from urllib.parse import urlsplit
from pathlib import Path

default_skill = Path(__file__).resolve().parents[4] / "skills" / "testops-rerun-triage"
skill = Path(os.environ.get("TESTOPS_CODE_MODE_TRIAGE_HOME", default_skill))
if not (skill / "scripts" / "classify_rerun.py").is_file():
    sys.exit(4)
os.environ["QA_HOME"] = str(skill)
sys.path.insert(0, str(skill / "scripts"))
import classify_rerun as triage
import testops_client as client


def main():
    cfg = triage.load_config(triage.artifacts_root())
    token = triage.resolve_token(cfg)
    if not token:
        return 4
    triage.cl.load_rules(triage.RULES_PATH)
    triage.init_state(cfg, token)
    class NoRedirect(client.urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            return None

    opener = client.urllib.request.build_opener(NoRedirect())
    origin = urlsplit(cfg["testops_base_url"])
    calls = 0
    received = 0
    exceeded = False

    class LimitedResponse:
        def __init__(self, response):
            self.response = response

        def __enter__(self):
            self.response.__enter__()
            return self

        def __exit__(self, *args):
            return self.response.__exit__(*args)

        def read(self):
            nonlocal exceeded, received
            limit = 4 * 1024 * 1024
            content = self.response.read(limit + 1)
            received += len(content)
            if len(content) > limit or received > 32 * 1024 * 1024:
                exceeded = True
                raise RuntimeError("Response limit")
            return content

    def limited_urlopen(request, **kwargs):
        nonlocal calls, exceeded
        if calls >= 512:
            exceeded = True
            raise RuntimeError("Request limit")
        calls += 1
        url = urlsplit(request.full_url)
        if (url.scheme, url.netloc) != (origin.scheme, origin.netloc) or not re.fullmatch(
            r"/api/(?:uaa/oauth/token|launch/\d+|v2/launch/\d+/test-result/flat|testresult/\d+)", url.path
        ):
            raise ValueError("Invalid TestOps route")
        try:
            return LimitedResponse(opener.open(request, **kwargs))
        except urllib.error.HTTPError as error:
            # The existing client reads the error body. Keep it out of the child response.
            error.close()
            raise urllib.error.HTTPError(error.url, error.code, error.reason, error.headers, io.BytesIO()) from None

    client.urllib.request.urlopen = limited_urlopen
    try:
        result = triage.collect(int(sys.argv[1]), with_trace=True, page_size=cfg.get("page_size", 100))
    except Exception:
        return 3 if exceeded else 1
    if exceeded:
        return 3
    print(json.dumps({"requests": calls, "result": result}, ensure_ascii=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
