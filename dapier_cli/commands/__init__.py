"""Implementations of the `dapier` commands, one module per noun.

The parser/dispatch layer (dapier_cli.cli) resolves handlers through this
package namespace at call time, so tests and callers can patch commands.<name>.
"""

from .. import api

from .bookkeeping import *
from .connections import *
from .triggers import *
from .workflows import *
from .hooks import *
from .schedules import *
from .polls import *
from .catalog import *
from .credentials import *
from .grants import *
from .tokens import *
from .overview import *
from .runs import *
from .audit import *
from .usage import *
from .inbox import *
from .storage import *
from .oauth_clients import *
from .emails import *
from .tasks import *
