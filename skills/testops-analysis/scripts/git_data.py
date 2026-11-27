#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""git_data.py — доступ к данным QA в репозитории AUTOTEST/sfa через git (stdlib).

Почему git, а не REST: в этом репозитории REST-запись файлов запрещена repository
hook'ом («Branch ... can only be modified through pull requests»), а чтение через
git partial+sparse clone стоит столько же, сколько REST (0.8 МБ объектов на ветку
автотестов против 224 МБ полного клона) и не требует персонального токена —
работают обычные git-креды оператора (Credential Manager / SSH / Jenkins-креды).

Две операции:
  ensure_cache() + read_files() — чтение каталога ветки в локальный кэш;
  push_files()                  — публикация файлов одним коммитом в ветку.

Кэш клона живёт в `<artifacts root>/_git/<project>-<repo>-<branch>/` и обновляется
инкрементально (`fetch --depth 1`), поэтому повторные чтения почти бесплатны.
"""
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


class GitError(Exception):
    """Ошибка git; code — предлагаемый код возврата процесса."""

    def __init__(self, message, code=1):
        super().__init__(message)
        self.code = code


def repo_url(base_url, project, repo):
    """HTTPS-URL репозитория Bitbucket Server по base URL и ключам проекта/репозитория."""
    host = str(base_url).split("://", 1)[-1].rstrip("/")
    return f"https://{host}/scm/{str(project).lower()}/{str(repo)}.git"


def resolve_source(cfg, defaults, overrides=None):
    """Параметры источника: CLI > env QA_<KEY> > config.json > defaults."""
    src = dict(defaults)
    for key in defaults:
        if (cfg or {}).get(key):
            src[key] = str(cfg[key])
        env_value = os.environ.get("QA_" + key.upper())
        if env_value:
            src[key] = env_value
    for key, value in (overrides or {}).items():
        if value:
            src[key] = value
    src["bitbucket_base_url"] = str(src["bitbucket_base_url"]).rstrip("/")
    src["path"] = str(src["path"]).strip("/")
    return src


def git_available():
    """Есть ли git в PATH (и какая версия)."""
    try:
        out = subprocess.run(["git", "--version"], capture_output=True, text=True, timeout=15)
        return out.stdout.strip() if out.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def _run(args, timeout, cwd=None):
    """git-команда без интерактивных запросов. Возвращает stdout."""
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    try:
        proc = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True,
                              timeout=timeout, env=env)
    except subprocess.TimeoutExpired:
        raise GitError(f"git {' '.join(args[:2])}: таймаут {timeout}s")
    except OSError as e:
        raise GitError(f"git не запускается: {e} (нужен git в PATH)")
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip().splitlines()
        detail = [d for d in detail if not d.lower().startswith("warning:")]
        tail = detail[-1] if detail else "нет вывода"
        brief = " ".join(detail[:2]) if detail else "нет вывода"
        joined = " ".join(detail).lower()
        if "couldn't find remote ref" in joined or "remote ref does not exist" in joined:
            raise GitError(f"в репозитории нет такой ветки: {brief}", code=4)
        if ("repository not found" in joined or "does not exist, or you do not have permission" in joined
                or "not authorized" in joined):
            raise GitError(
                "репозиторий не найден или нет доступа (проверь project/repo и git-креды): "
                + brief, code=2)
        if ("non-fast-forward" in joined or "fetch first" in joined or "[rejected]" in joined):
            raise GitError("кто-то опубликовал в эту ветку раньше (non-fast-forward): "
                           "перечитываю базу и повторяю", code=5)
        if ("could not read username" in joined or "authentication" in joined
                or "permission denied (publickey)" in joined or "terminal prompts disabled" in joined
                or "returned error: 403" in joined or "returned error: 401" in joined
                or "you are not allowed to upload" in joined):
            raise GitError(
                f"нет доступа к репозиторию (git-креды не настроены или нет прав): {brief}", code=2)
        raise GitError(f"git {' '.join(args[:2])}: {brief}")
    return proc.stdout.strip()


def cache_dir(root, project, repo, branch):
    """Каталог постоянного кэша клона для пары репозиторий+ветка."""
    safe = f"{project}-{repo}-{branch}".replace("/", "_")
    return Path(root) / "_git" / safe


def ensure_cache(cache, url, branch, sparse_path, timeout=300):
    """Клон/обновление ветки в кэше. Возвращает путь к рабочему дереву кэша."""
    cache = Path(cache)
    if not (cache / ".git").exists():
        cache.parent.mkdir(parents=True, exist_ok=True)
        if cache.exists():
            # остаток от прерванного клона
            for child in sorted(cache.iterdir(), reverse=True):
                if child.is_dir() and not child.is_symlink():
                    shutil.rmtree(child, ignore_errors=True)
                else:
                    child.unlink(missing_ok=True)
        _run(["clone", "-q", "--filter=blob:none", "--sparse", "--depth", "1",
              "-b", branch, url, str(cache)], timeout)
    else:
        _run(["-C", str(cache), "fetch", "-q", "--depth", "1", "origin", branch], timeout)
        _run(["-C", str(cache), "checkout", "-q", "-f", "FETCH_HEAD"], timeout)
    if sparse_path:
        _run(["-C", str(cache), "sparse-checkout", "set", sparse_path], timeout)
    return cache


def read_files(cache, url, branch, sparse_path, paths, timeout=300):
    """Читает файлы ветки: {путь: bytes|None}. Отсутствующие — None."""
    work = ensure_cache(cache, url, branch, sparse_path, timeout)
    result = {}
    for rel in paths:
        target = Path(work) / rel
        result[rel] = target.read_bytes() if target.is_file() else None
    return result


def list_tree(cache, url, branch, timeout=300):
    """Все пути ветки без выкачки блобов (git ls-tree -r --name-only).

    Нужен там, где важен факт наличия файла и его каталог, а содержимое не нужно:
    например, поиск `.feature` теста и проверка, не лежит ли он в отключённой
    папке (`features/excluded/`). Не требует sparse-checkout и не тянет блобы.
    """
    cache = Path(cache)
    if not (cache / ".git").exists():
        cache.parent.mkdir(parents=True, exist_ok=True)
        _run(["clone", "-q", "--filter=blob:none", "--no-checkout", "--depth", "1",
              "-b", branch, url, str(cache)], timeout)
    else:
        _run(["-C", str(cache), "fetch", "-q", "--depth", "1", "origin", branch], timeout)
        _run(["-C", str(cache), "checkout", "-q", "-f", "FETCH_HEAD"], timeout)
    out = _run(["-C", str(cache), "ls-tree", "-r", "--name-only", "HEAD"], timeout)
    return [line for line in out.splitlines() if line]


def list_dirs(cache, url, branch, sparse_path, sub_path="", timeout=300):
    """Подкаталоги внутри sparse-каталога (для --list)."""
    work = ensure_cache(cache, url, branch, sparse_path, timeout)
    base = Path(work) / sparse_path / sub_path
    if not base.is_dir():
        return []
    return sorted(p.name for p in base.iterdir() if p.is_dir())


def head_commit(cache, timeout=60):
    """Короткий SHA текущего HEAD кэша."""
    return _run(["-C", str(cache), "rev-parse", "--short", "HEAD"], timeout)


def push_files(url, branch, files, message, timeout=300, create_if_missing=False):
    """Коммитит файлы в ветку одним коммитом.

    files: {путь в репозитории: bytes}. Возвращает короткий SHA или 'no-changes'.
    create_if_missing=False — отсутствие ветки это ошибка (защита от опечатки в имени:
    иначе создаётся мусорная ветка в общем репозитории).
    """
    with tempfile.TemporaryDirectory(prefix="qa-data-") as tmp:
        _run(["init", "-q"], timeout, cwd=tmp)
        _run(["remote", "add", "origin", url], timeout, cwd=tmp)
        existing = True
        try:
            _run(["fetch", "-q", "--depth", "1", "origin", branch], timeout, cwd=tmp)
            _run(["checkout", "-q", "-B", "work", "FETCH_HEAD"], timeout, cwd=tmp)
        except GitError as e:
            # Только «нет такой ветки» — повод создать её. Ошибки доступа/репозитория
            # пробрасываем: иначе опечатка в имени ветки создаст мусор в общем репо.
            if e.code != 4:
                raise
            if not create_if_missing:
                raise GitError(
                    f"ветки {branch} нет в репозитории. Если она правда нужна — "
                    f"создай её через --create-branch", code=4)
            existing = False
            _run(["checkout", "-q", "--orphan", "work"], timeout, cwd=tmp)
        for rel, data in files.items():
            target = Path(tmp) / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        _run(["add", "-A"], timeout, cwd=tmp)
        if not _run(["status", "--porcelain"], timeout, cwd=tmp):
            return "no-changes"
        _run(["commit", "-q", "-m", message], timeout, cwd=tmp)
        _run(["push", "-q", "origin", f"HEAD:refs/heads/{branch}"], timeout, cwd=tmp)
        if not existing:
            print(f"  [i] ветка {branch} создана заново (orphan)", file=sys.stderr)
        return _run(["rev-parse", "--short", "HEAD"], timeout, cwd=tmp)
