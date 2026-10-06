"""Complete schema registry for migrations and tests, independent of service startup."""

from ..artifacts import models as artifact_models  # noqa: F401
from ..auth import models as auth_models  # noqa: F401
from ..network import models as network_models  # noqa: F401
from ..notifications import models as notification_models  # noqa: F401
from ..transfers import models as transfer_models  # noqa: F401
from .models import Base

__all__ = ["Base"]
