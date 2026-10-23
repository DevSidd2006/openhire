"""Static regressions for pages that fetch authenticated data on load.

The auth guard finishes an OAuth ticket exchange before it resolves. A page
that calls it without awaiting can issue its first authenticated request
while that exchange is still in flight, so these pages wrap their initial
work in an async function and await the guard first.
"""
import re
from pathlib import Path

import pytest

_PAGES = (
    "pages/jobs.html",
    "pages/mock-interviews.html",
)


@pytest.mark.parametrize("page", _PAGES)
def test_page_awaits_auth_guard_before_its_initial_requests(page: str) -> None:
    source = Path(page).read_text(encoding="utf-8")

    assert "await guardAuthenticatedPage()" in source
    assert not re.search(r"(?<!await )guardAuthenticatedPage\(\);", source)
