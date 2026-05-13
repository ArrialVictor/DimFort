# Importing unit_config has the side effect of populating
# dimfort.core.units.DEFAULT_TABLE so that bare `units.parse(expr)` works.
from dimfort.core import unit_config  # noqa: F401
