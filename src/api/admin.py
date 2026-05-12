"""Admin API routes — thin hub that merges all admin sub-module routers.

Sub-modules:
  admin_auth.py     — login / logout / password
  admin_tokens.py   — token CRUD
  admin_config.py   — proxy / generation / cache / logs / health
  admin_captcha.py  — captcha config + score test
  admin_plugin.py   — plugin config + extension token push
  admin_utils.py    — shared helper functions (no endpoints)
  _admin_deps.py    — shared dependency injection state
"""
from fastapi import APIRouter

from . import _admin_deps
from . import admin_auth
from . import admin_tokens
from . import admin_config
from . import admin_captcha
from . import admin_plugin

router = APIRouter()

# Merge all sub-module routers
router.include_router(admin_auth.router)
router.include_router(admin_tokens.router)
router.include_router(admin_config.router)
router.include_router(admin_captcha.router)
router.include_router(admin_plugin.router)


def set_dependencies(tm, pm, database, cm=None):
    """Distribute service instances to all admin sub-modules."""
    _admin_deps.set_all(tm, pm, database, cm)
