"""Single source of truth for the Claude Usage Widget version.

Bump this and only this. The release flow takes care of the rest:

  Python entry scripts (claude_usage_tray.py, claude_usage_menubar.py)
    -> from _version import __version__

  Inno Setup installer (installer.iss)
    -> #ifndef AppVersion fallback; the real value is passed at compile
       time as /DAppVersion=<value>, sourced from this module by both
       the CI workflow (.github/workflows/build-windows.yml) and the
       local helper script (rebuild_and_install.bat).

The browser extension's manifest.json is intentionally NOT kept in sync
with this value. Bump the extension version independently when the
extension's own code or permissions change, not on every widget release.
That keeps the user-visible "extension version" stable across cosmetic
widget releases and tells them something actually shifted on the
extension side when it does change.
"""
__version__ = "1.3.3"
