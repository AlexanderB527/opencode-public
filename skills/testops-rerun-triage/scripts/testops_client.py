"""HTTP-клиент TestOps API (OAuth apitoken -> JWT + Bearer).

Самодостаточный модуль: скилл не импортирует скрипты других скиллов.
Конфиг: env TESTOPS_TOKEN / TESTOPS_BASE_URL, либо ~/.qa-artifacts/config.json.
"""

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

_STATE = {"jwt": None, "base": "", "token": "", "cfg": {}}


def load_config(artifacts_root: Path) -> dict:
    """Читает config.json корня артефактов; дефолты — боевой TestOps."""
    cfg = {
        "testops_base_url": "https://example.invalid",
        "testops_token_env": "TESTOPS_TOKEN",
        "testops_token": "",
        "page_size": 100,
    }
    cfg_path = Path(artifacts_root) / "config.json"
    if cfg_path.exists():
        try:
            cfg.update(json.loads(cfg_path.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, OSError):
            pass
    return cfg


def resolve_token(cfg: dict) -> str:
    """Токен: env (имя из конфига) -> поле testops_token в конфиге."""
    token = os.environ.get(cfg.get("testops_token_env", "TESTOPS_TOKEN"), "")
    if not token:
        token = cfg.get("testops_token", "")
    return str(token or "").strip()


def init_state(cfg: dict, token: str) -> None:
    """Заполняет состояние клиента (base/token). JWT берётся лениво."""
    _STATE["cfg"] = cfg
    _STATE["token"] = token
    _STATE["base"] = str(cfg["testops_base_url"]).rstrip("/")
    _STATE["jwt"] = None


def _acquire_jwt() -> str:
    """POST /api/uaa/oauth/token (grant_type=apitoken, scope=openid) -> access_token."""
    url = f"{_STATE['base']}/api/uaa/oauth/token"
    body = urllib.parse.urlencode(
        {"grant_type": "apitoken", "scope": "openid", "token": _STATE["token"]}
    ).encode("utf-8")
    headers = {"Accept": "application/json",
               "Content-Type": "application/x-www-form-urlencoded"}
    req = urllib.request.Request(url, data=body, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise RuntimeError(
            f"OAuth token не получен: HTTP {e.code}: "
            f"{e.read().decode('utf-8', errors='replace')[:300]}"
        )
    jwt = str(data.get("access_token") or "")
    if not jwt:
        raise RuntimeError("OAuth: в ответе нет access_token")
    return jwt


def get_json(path_or_url: str, retries: int = 1):
    """GET с Bearer. При 401 — переполучить JWT и повторить (1 раз)."""
    url = path_or_url if path_or_url.startswith("http") else _STATE["base"] + path_or_url
    last_err = None
    for _ in range(retries + 1):
        if not _STATE["jwt"]:
            _STATE["jwt"] = _acquire_jwt()
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {_STATE['jwt']}"})
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            last_err = f"HTTP {e.code}: {e.read().decode('utf-8', errors='replace')[:300]}"
            if e.code == 401:
                _STATE["jwt"] = None
                continue
            break
        except Exception as e:  # noqa: BLE001 — сеть не должна ронять вызывающий код молча
            last_err = str(e)
    raise RuntimeError(f"GET {url} не удался: {last_err}")


def get_launch(launch_id: int) -> dict:
    """Метаданные запуска: name, projectId, closed, dates."""
    return get_json(f"/api/launch/{launch_id}")


def scan_flat(launch_id: int, page_size: int = 100) -> tuple:
    """Постраничный скан результатов запуска: (rows, info).

    size=100 — максимум API; больше молча обрезает вывод. info.complete — гейт полноты.
    """
    size = min(int(page_size or 100), 100)
    rows, page, processed, total = [], 0, 0, None
    while True:
        data = get_json(f"/api/v2/launch/{launch_id}/test-result/flat?page={page}&size={size}")
        content = data.get("content") or []
        if total is None:
            total = int(data.get("totalElements") or len(content))
        if not content:
            break
        rows.extend(content)
        processed += len(content)
        if processed >= total:
            break
        page += 1
        if page > 10_000:
            raise RuntimeError("Пагинация превысила 10_000 страниц — total не сходится")
    return rows, {"total": total or 0, "pages": page + 1, "rows_scanned": processed,
                  "complete": processed == (total or 0)}


def get_result(result_id) -> dict:
    """Детали результата: message, trace, testCaseId, flaky, jobRun, tags."""
    return get_json(f"/api/testresult/{result_id}")
