"""How Plaid's institution logo becomes something a browser can render.

Plaid's field is called `logo`, not `logo_url`, and means it: the value is a
base64-encoded PNG rather than a link to one. Copying it straight into a
column named `logo_url` produced a value that was neither -- too large for the
column, and unusable as an image source even when it fit.
"""

from __future__ import annotations

from app.services.plaid_gateway import _logo_to_data_uri


def test_base64_becomes_a_data_uri():
    assert _logo_to_data_uri("iVBORw0KGgo") == "data:image/png;base64,iVBORw0KGgo"


def test_missing_logo_stays_none():
    """Not every institution publishes one, and that is not an error."""
    assert _logo_to_data_uri(None) is None
    assert _logo_to_data_uri("") is None


def test_an_actual_url_is_left_alone():
    """Defensive: Plaid returning a real link must not be double-wrapped.

    Wrapping a URL in a data: prefix would produce a string that looks
    plausible and renders as nothing.
    """
    url = "https://example.com/logo.png"
    assert _logo_to_data_uri(url) == url


def test_an_existing_data_uri_is_not_wrapped_twice():
    already = "data:image/png;base64,iVBORw0KGgo"
    assert _logo_to_data_uri(already) == already
