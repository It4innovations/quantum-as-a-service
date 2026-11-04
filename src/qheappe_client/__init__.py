from .backend_metadata import LexisResource, QBackendMetadata
from .iqm_compat import IQMStack, QBackendIQMFactory
from .iqm_compat.stack import iqm_import_error_message, is_iqm_extra_import_error

# These modules need Qiskit and the IQM libraries, which come from an optional extra.
# Turn a missing or unsupported install into an error that says what to install.
try:
    from .qiskit.qbackend import QBackend, QJob
    from .qiskit.qbackend_iqm import QBackendIQM
    from .qprovider import QProvider
except ImportError as e:
    if not is_iqm_extra_import_error(e):
        raise
    raise ImportError(iqm_import_error_message(e), name=e.name) from e

from .qpulla import QPulla

__all__ = [
    "IQMStack",
    "LexisResource",
    "QBackend",
    "QBackendIQM",
    "QBackendIQMFactory",
    "QBackendMetadata",
    "QJob",
    "QProvider",
    "QPulla",
]
