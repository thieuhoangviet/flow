"""Shared dependencies for admin sub-modules.

All admin sub-modules import their service references from here
to avoid circular imports and centralise dependency injection.
"""

token_manager = None
proxy_manager = None
db = None
concurrency_manager = None


def set_all(tm, pm, database, cm=None):
    """Called once from admin.set_dependencies() during startup."""
    global token_manager, proxy_manager, db, concurrency_manager
    token_manager = tm
    proxy_manager = pm
    db = database
    concurrency_manager = cm
