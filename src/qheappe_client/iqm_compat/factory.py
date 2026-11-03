"""Factory that picks the IQM stack adapter and backend class for a backend.

The installed IQM libraries decide which adapter *can* be loaded; the
``station_control_version`` reported by ``/heappe/QScheduler/MachineInfo``
decides which one the backend *needs*. The factory checks they match and
builds the matching :class:`~qheappe_client.qiskit.qbackend_iqm.QBackendIQM`
subclass.
"""

from __future__ import annotations

import importlib
import logging
from types import ModuleType
from typing import TYPE_CHECKING, Any
from uuid import UUID

from ..backend_metadata import QBackendMetadata, QMachineInfo
from ..utils import QException
from .stack import (
    IQMStack,
    install_hint,
    installed_iqm_client_version,
    installed_stack,
    required_stack,
)

if TYPE_CHECKING:
    from ..qclient import QClient
    from ..qiskit.qbackend_iqm import QBackendIQM

log = logging.getLogger(__name__)

# stack -> name of the backend class in its adapter module
_BACKEND_CLASS_NAME: dict[IQMStack, str] = {
    IQMStack.IQM35: "QBackendIQM35",
    IQMStack.IQM36: "QBackendIQM36",
}


class QBackendIQMFactory:
    """Create IQM backends that match both the installed IQM stack and the server.

    Example::

        backend = QBackendIQMFactory.create(client, backend_metadata)
        backend.iqm_stack  # IQMStack.IQM36

    All methods are class methods; the class holds no state.
    """

    @classmethod
    def installed_stack(cls) -> IQMStack:
        """Stack of the installed ``iqm-client`` (see :func:`.stack.installed_stack`)."""
        return installed_stack()

    @classmethod
    def required_stack(cls, machine_info: QMachineInfo | None) -> IQMStack | None:
        """Stack the backend needs, or ``None`` if unknown (see :func:`.stack.required_stack`)."""
        return required_stack(machine_info)

    @classmethod
    def resolve_stack(
        cls,
        machine_info: QMachineInfo | None,
        *,
        backend_name: str | None = None,
        strict: bool = True,
    ) -> IQMStack:
        """Pick the stack to use for a backend.

        Args:
            machine_info: Parsed ``MachineInfo`` of the backend.
            backend_name: Used in messages only.
            strict: Raise on a mismatch between installed and required stack.
                If ``False``, log a warning and use the installed stack.

        Returns:
            The installed stack (it is the only one that can be imported).

        Raises:
            QException: No supported ``iqm-client`` installed, or (``strict``)
                the backend needs a different stack than the installed one.
        """
        installed = installed_stack()
        required = required_stack(machine_info)
        name = backend_name or "<backend>"
        reported = machine_info.station_control_version if machine_info else None

        if required is None:
            log.warning(
                "Backend '%s' reports station_control_version=%r; cannot tell which IQM "
                "stack it needs. Using the installed one (%s).",
                name,
                reported,
                installed,
            )
            return installed

        if required != installed:
            msg = (
                f"Backend '{name}' runs IQM station-control {reported}, which needs the "
                f"{required.value} IQM stack, but iqm-client {installed_iqm_client_version()} "
                f"({installed.value}) is installed. Install the matching libraries with: "
                f"{install_hint(required)}"
            )
            if strict:
                raise QException(msg)
            log.warning("%s. Continuing with %s (strict=False).", msg, installed.value)
        return installed

    @classmethod
    def adapter(cls, stack: IQMStack | None = None) -> ModuleType:
        """Import the adapter module of a stack.

        Args:
            stack: Stack to load; the installed one if ``None``.

        Returns:
            The ``qheappe_client.iqm_compat.iqm35`` / ``iqm36`` module.

        Raises:
            QException: The stack's libraries are not installed.
        """
        stack = stack or installed_stack()
        try:
            return importlib.import_module(f"{__package__}.{stack.value}")
        except ImportError as e:
            raise QException(
                f"Cannot load the {stack.value} IQM stack ({e}). "
                f"Install it with: {install_hint(stack)}"
            ) from e

    @classmethod
    def backend_class(cls, stack: IQMStack | None = None) -> type[QBackendIQM]:
        """Backend class of a stack (the installed one if ``None``)."""
        stack = stack or installed_stack()
        return getattr(cls.adapter(stack), _BACKEND_CLASS_NAME[stack])

    @classmethod
    def create(
        cls,
        client: QClient,
        backend_metadata: QBackendMetadata,
        calibration_set_id: UUID | str | None = None,
        *,
        use_metrics: bool = False,
        strict: bool = True,
        **kwargs: Any,
    ) -> QBackendIQM:
        """Build the IQM backend matching the installed stack and the server.

        Args:
            client: Client bound to the backend.
            backend_metadata: Metadata with the dynamic architecture loaded.
            calibration_set_id: Calibration set the architecture was fetched for.
            use_metrics: Load calibration metrics into the transpiler target.
            strict: See :meth:`resolve_stack`.
            **kwargs: Passed to the backend constructor.

        Returns:
            A ``QBackendIQM35`` or ``QBackendIQM36`` instance.

        Raises:
            QException: See :meth:`resolve_stack` and :meth:`adapter`.
            ValueError: The metadata has no dynamic architecture.
        """
        if backend_metadata.dynamic_architecture is None:
            raise ValueError(
                f"Backend '{backend_metadata.backend_name}' has no dynamic architecture."
            )
        stack = cls.resolve_stack(
            backend_metadata.machine_info,
            backend_name=backend_metadata.backend_name,
            strict=strict,
        )
        backend_cls = cls.backend_class(stack)
        return backend_cls(
            client=client,
            architecture=backend_metadata.dynamic_architecture,
            use_metrics=use_metrics,
            backend_metadata=backend_metadata,
            calibration_set_id=calibration_set_id,
            **kwargs,
        )
