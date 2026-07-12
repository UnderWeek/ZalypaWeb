"""Core domain services used by the Auralis Browser UI."""

from .profiles import BrowserProfile, Profile, ProfileManager, ProfilePaths
from .security import (
    PermissionDecision,
    PermissionType,
    SecurityManager,
    SecurityVerdict,
    SitePermission,
    SitePermissionStore,
    URLRisk,
)

__all__ = [
    "BrowserProfile",
    "PermissionDecision",
    "PermissionType",
    "Profile",
    "ProfileManager",
    "ProfilePaths",
    "SecurityManager",
    "SecurityVerdict",
    "SitePermission",
    "SitePermissionStore",
    "URLRisk",
]
