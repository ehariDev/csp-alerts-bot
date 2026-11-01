"""Local, authenticated, READ-ONLY dashboard for the CSP alerts journal (WEB-1).

Separate package on purpose: `csp_alerts` has no server socket and no inbound path (acceptance-tested); this
process only reads the persisted journal (SQLite opened read-only), never scans, sends, migrates or writes.
"""
__version__ = "0.1.0"
