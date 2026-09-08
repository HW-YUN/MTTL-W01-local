#!/usr/bin/env python3
"""
엔진(relay.py)에는 기기별 하드코딩 문자열이 0줄이어야 한다는 원칙 그대로 — "무엇에 어떻게
응답할지"는 전부 `rules/*.py` 소관. 이 파일 자체도 그 원칙을 지킨다.

rules/ 디렉터리 안의 *.py 전부를 파일명 알파벳순으로 로드(핫리로드 — mtime 변경 감지, 몇 초
안에 반영). 우선순위 = 파일명 순서(먼저 매칭되는 파일이 이김) — 최저우선순위(fallback)로 두고
싶은 파일은 `99-` 처럼 뒤로 정렬되는 이름을 쓸 것(예: `99-default.py`).

각 파일은 아래 함수들 중 필요한 것만 정의하면 됨(전부 optional):

  on_http_request(ctx, method, path, headers, body) -> HttpResponse | None
      :80/:443 HTTP 요청. 파일 순서대로 첫 non-None 이 이김.
  on_message(ctx, msg, topic) -> list[PublishSpec] | None
      기기->서버 oneM2M 요청 PUBLISH. 파일 순서대로 첫 non-None 이 이김.
  on_session_start(ctx) -> list[PublishSpec] | None
      기기의 SUBSCRIBE 완료 직후. 전부 호출됨, 결과 전부 이어붙여서 순서대로 전송.
  on_local_inject(ctx, cmd) -> list[PublishSpec] | None
      observer 로컬 주입 명령 -> 기기 세션에 넣을 메시지로 번역. 첫 non-None 이 이김.
  on_tick(ctx, now_ms) -> list[PublishSpec] | None
      주기 타이머(선택, 모듈 최상단 TICK_SECS 상수로 자기 간격 선언, 기본 60). 전부 호출.

스크립트(모듈) 로드/실행 중 예외가 나면 크래시 대신 그 파일만 스킵하고 stderr 경고
(fail-closed) — 엔진 안정성을 rules 버그가 깨지 않게.
"""

from __future__ import annotations

import importlib.util
import logging
import os
import threading
from dataclasses import dataclass
from typing import Optional

# TRACE < DEBUG — 매 이벤트/매 패킷 단위로 무조건 찍히는 가장 시끄러운 레벨.
# 기본(INFO)에서는 안 보이고 --log-level trace로 켰을 때만 나온다.
TRACE = 5
logging.addLevelName(TRACE, "TRACE")


def _trace(self, message, *args, **kwargs):
    if self.isEnabledFor(TRACE):
        self._log(TRACE, message, args, **kwargs)


logging.Logger.trace = _trace

LOG_LEVELS = {
    "TRACE": TRACE,
    "DEBUG": logging.DEBUG,
    "INFO": logging.INFO,
    "WARNING": logging.WARNING,
    "ERROR": logging.ERROR,
    "CRITICAL": logging.CRITICAL,
}

_log = logging.getLogger("rules_engine")


@dataclass
class StreamPlan:
    """LOCAL PATCH (see LOCAL_PATCHES.md): optional paced file stream for an
    HttpResponse. When HttpResponse.stream is set, serve_http_locally sends the
    headers and then streams `path` from disk in `chunk`-byte reads (never loading
    the whole file into memory), sleeping `pace` seconds between chunks. After the
    transfer ends — completed OR interrupted — it calls on_complete(sent, filesize)
    exactly once; the caller decides success by `sent == filesize`.
    A rule that never sets HttpResponse.stream is completely unaffected."""
    path: str
    chunk: int = 4096
    pace: float = 0.0
    on_complete: "Optional[object]" = None  # callable(sent: int, filesize: int) -> None


@dataclass
class HttpResponse:
    status_line: bytes  # e.g. b"HTTP/1.1 200 OK"
    headers: dict        # {"Content-Type": "application/xml;charset=UTF-8", ...}
    body: bytes
    stream: "Optional[StreamPlan]" = None  # LOCAL PATCH: None -> unchanged (body is sent);
                                           # set -> serve_http_locally streams the file instead.


@dataclass
class PublishSpec:
    topic: bytes
    payload: bytes
    qos: int = 1


@dataclass
class _Module:
    path: str
    mtime: float
    mod: object


class RulesHandle:
    """rules_dir 안의 *.py 를 로드/핫리로드하고, 훅을 순서대로 호출해주는 디스패처."""

    def __init__(self, rules_dir: Optional[str]):
        self.rules_dir = rules_dir
        self._modules: dict[str, _Module] = {}
        self._lock = threading.Lock()
        if rules_dir:
            self._reload()

    @staticmethod
    def empty() -> "RulesHandle":
        return RulesHandle(None)

    def _reload(self):
        if not self.rules_dir or not os.path.isdir(self.rules_dir):
            return
        files = sorted(f for f in os.listdir(self.rules_dir) if f.endswith(".py"))
        seen = set()
        with self._lock:
            for fname in files:
                path = os.path.join(self.rules_dir, fname)
                seen.add(path)
                try:
                    mtime = os.path.getmtime(path)
                except OSError:
                    continue
                existing = self._modules.get(path)
                if existing and existing.mtime == mtime:
                    continue
                try:
                    spec = importlib.util.spec_from_file_location(f"rules_{fname[:-3]}", path)
                    mod = importlib.util.module_from_spec(spec)
                    spec.loader.exec_module(mod)
                    self._modules[path] = _Module(path=path, mtime=mtime, mod=mod)
                    _log.info("로드: %s", fname)
                except Exception:
                    _log.exception("%s 로드 실패(스킵)", fname)
            for path in list(self._modules):
                if path not in seen:
                    del self._modules[path]

    def _ordered_mods(self):
        self._reload()
        with self._lock:
            return [self._modules[p].mod for p in sorted(self._modules)]

    def _call_first(self, hook_name: str, *args):
        for mod in self._ordered_mods():
            fn = getattr(mod, hook_name, None)
            if fn is None:
                continue
            try:
                result = fn(*args)
            except Exception:
                _log.exception("%s 실행 중 예외(스킵)", hook_name)
                continue
            if result is not None:
                return result
        return None

    def _call_all(self, hook_name: str, *args) -> list:
        out = []
        for mod in self._ordered_mods():
            fn = getattr(mod, hook_name, None)
            if fn is None:
                continue
            try:
                result = fn(*args)
            except Exception:
                _log.exception("%s 실행 중 예외(스킵)", hook_name)
                continue
            if result:
                out.extend(result)
        return out

    def on_http_request(self, ctx, method, path, headers, body) -> Optional[HttpResponse]:
        return self._call_first("on_http_request", ctx, method, path, headers, body)

    def on_message(self, ctx, msg, topic) -> list:
        return self._call_first("on_message", ctx, msg, topic) or []

    def on_session_start(self, ctx) -> list:
        return self._call_all("on_session_start", ctx)

    def on_local_inject(self, ctx, cmd) -> Optional[list]:
        return self._call_first("on_local_inject", ctx, cmd)

    def on_tick(self, ctx, now_ms: int) -> list:
        out = []
        for mod in self._ordered_mods():
            fn = getattr(mod, "on_tick", None)
            if fn is None:
                continue
            interval = getattr(mod, "TICK_SECS", 60) * 1000
            last = getattr(mod, "_last_tick_ms", 0)
            if now_ms - last < interval:
                continue
            try:
                result = fn(ctx, now_ms)
                mod._last_tick_ms = now_ms
            except Exception:
                _log.exception("on_tick 실행 중 예외(스킵)")
                continue
            if result:
                out.extend(result)
        return out
