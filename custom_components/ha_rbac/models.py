"""Shared runtime types for the RBAC integration."""

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .catalog import Catalog
    from .denylog import DenyLog
    from .policy import Evaluator
    from .proxy import RbacProxy
    from .store import RbacStore


@dataclass(slots=True)
class RbacData:
    """Runtime state, stored on the config entry."""

    store: "RbacStore"
    catalog: "Catalog"
    evaluator: "Evaluator"
    denylog: "DenyLog"
    proxy: "RbacProxy | None" = None
    unsubscribes: list[Any] = field(default_factory=list)
