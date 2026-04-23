"""
harness.py — Shared test harness for tos-api and tos-dash-v2 PVT suites.
"""

import time
from typing import Callable

import requests

_GREEN  = "\033[92m"
_RED    = "\033[91m"
_YELLOW = "\033[93m"
_DIM    = "\033[2m"
_RESET  = "\033[0m"


class Harness:
    def __init__(self, base_url: str, label: str = ""):
        self.base_url = base_url.rstrip("/")
        self.label    = label
        self.passed   = 0
        self.failed   = 0
        self.skipped  = 0

    def run(self, name: str, fn: Callable[[], None]):
        t0 = time.perf_counter()
        try:
            fn()
            ms = round((time.perf_counter() - t0) * 1000, 1)
            print(f"  {_GREEN}PASS{_RESET}  {name} {_DIM}({ms}ms){_RESET}")
            self.passed += 1
        except AssertionError as e:
            ms = round((time.perf_counter() - t0) * 1000, 1)
            print(f"  {_RED}FAIL{_RESET}  {name} {_DIM}({ms}ms){_RESET}")
            print(f"        {_RED}{e}{_RESET}")
            self.failed += 1
        except Exception as e:
            ms = round((time.perf_counter() - t0) * 1000, 1)
            print(f"  {_RED}ERROR{_RESET} {name} {_DIM}({ms}ms){_RESET}")
            print(f"        {_RED}{type(e).__name__}: {e}{_RESET}")
            self.failed += 1

    def skip(self, name: str, reason: str):
        print(f"  {_YELLOW}SKIP{_RESET}  {name} {_DIM}({reason}){_RESET}")
        self.skipped += 1

    def get(self, path: str, timeout: int = 30) -> dict:
        r = requests.get(self.base_url + path, timeout=timeout)
        assert r.status_code == 200, \
            f"GET {path} -> {r.status_code}: {r.text[:200]}"
        try:
            return r.json()
        except Exception as e:
            raise AssertionError(f"GET {path} returned non-JSON: {e}")

    def post(self, path: str, body: dict, timeout: int = 60) -> dict:
        r = requests.post(self.base_url + path, json=body, timeout=timeout)
        assert r.status_code == 200, \
            f"POST {path} -> {r.status_code}: {r.text[:200]}"
        try:
            return r.json()
        except Exception as e:
            raise AssertionError(f"POST {path} returned non-JSON: {e}")

    def print_header(self):
        label = f" [{self.label}]" if self.label else ""
        print(f"\n{'='*60}")
        print(f"  {self.base_url}{label}")
        print(f"{'='*60}")

    def print_summary(self) -> int:
        total = self.passed + self.failed + self.skipped
        print()
        if self.failed == 0:
            print(f"{_GREEN}ALL PASS{_RESET}  "
                  f"{self.passed}/{total}  (skipped: {self.skipped})")
            return 0
        print(f"{_RED}FAILURES{_RESET}  "
              f"{self.passed} pass / {self.failed} fail / "
              f"{self.skipped} skip / {total} total")
        return 1
