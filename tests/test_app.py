"""Tests for the claude-tools-dashboard Flask app and its helpers."""


def test_import_app():
    """Smoke test: the app module imports cleanly and exposes its Flask app."""
    import app

    assert app.app is not None
    assert hasattr(app, "_collector")
