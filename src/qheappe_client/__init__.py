from typing import TYPE_CHECKING

from .backend_metadata import LexisResource, QBackendMetadata
from .iqm_compat import IQMStack, QBackendIQMFactory
from .iqm_compat.stack import iqm_import_error_message, is_iqm_extra_import_error

# These modules need Qiskit and the IQM libraries, which come from an optional extra.
# Turn a missing or unsupported install into an error that says what to install.
try:
    from .qiskit.qbackend import QBackend, QJob
    from .qiskit.qbackend_iqm import QBackendIQM
    from .qiskit.qsampler import QSamplerJob, QSamplerV2
    from .qprovider import QProvider
except ImportError as e:
    if not is_iqm_extra_import_error(e):
        raise
    raise ImportError(iqm_import_error_message(e), name=e.name) from e

if TYPE_CHECKING:
    from .qpulla import QPulla


def __getattr__(name: str):
    # QPulla needs the IQM 36 stack; import it on first use only.
    if name == "QPulla":
        from .qpulla import QPulla

        return QPulla
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


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
    "QSamplerJob",
    "QSamplerV2",
]
