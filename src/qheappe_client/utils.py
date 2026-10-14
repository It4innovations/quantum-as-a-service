import datetime
from collections import Counter
from enum import Enum
from uuid import UUID

import jwt
import requests
from iqm.iqm_server_client.models import JobStatus
from iqm.pulla.interface import HERALDING_KEY
from iqm.pulla.pulla import PullaJob
from iqm.pulla.utils_qiskit import qiskit_to_pulla as _iqm_qiskit_to_pulla
from iqm.qiskit_iqm import IQMJob
from qiskit.result import Counts, Result

# ------------
# Exceptions
# ------------


class QException(Exception):
    """
    Base exception for QHEAppE.

    All custom exceptions in the QHEAppE project should inherit from this class.
    Provides optional context info like user_id, resource, or additional data.

    Args:
        message: Optional human-readable error message.
        **context: Optional context info (e.g., user_id, resource).
    """

    def __init__(self, message=None, **context):
        self.context = context
        msg = message or self.__class__.__name__
        if context:
            # Include context in the exception message
            context_str = ", ".join(f"{k}={v}" for k, v in context.items())
            msg = f"{msg} ({context_str})"
        super().__init__(msg)


class QAuthException(QException):
    """
    Raised when a user is not authorized to perform an action.

    Possible reasons:
    - Invalid JWT token
    - User has no project assigned
    - User is requesting a non-existing resource

    Usage:
        raise QAuthException("No access", user_id=..., resource=...)
        raise QAuthException.from_cause(e, resource=...) from None   # reason derived from e

    Args:
        reason: Optional explanation of the authorization failure.
        user_id: Optional ID of the user.
        resource: Optional resource the user tried to access.
        cause: Optional underlying exception; used to derive `reason` when not given
            (note: `raise ... from e` sets __cause__ only after __init__, so pass it here).
    """

    DEFAULT_ERR_MSG = "Authentication error"

    def __init__(self, reason=None, user_id=None, resource=None, cause=None):
        if isinstance(cause, QAuthException):
            # Re-wrapping a QAuthException: keep its details unless overridden
            reason = reason or cause.reason
            user_id = user_id or cause.user_id
            resource = resource or cause.resource
        elif reason is None and cause is not None:
            reason = self._extract_reason_from_cause(cause)

        self.reason = reason or QAuthException.DEFAULT_ERR_MSG
        self.user_id = user_id
        self.resource = resource

        msg = self.reason
        if user_id:
            msg += f" (user_id={user_id})"
        if resource:
            msg += f", resource={resource}"

        super().__init__(msg)

    @classmethod
    def from_cause(
        cls, cause: BaseException, user_id=None, resource=None
    ) -> "QAuthException":
        """Create the exception with a human-readable reason derived from `cause`.

        Args:
            cause: Underlying exception.
            user_id: Optional ID of the user.
            resource: Optional resource the user tried to access.

        Returns:
            QAuthException instance with a human-readable reason.
        """
        return cls(user_id=user_id, resource=resource, cause=cause)

    @staticmethod
    def _extract_reason_from_cause(cause: BaseException) -> str:
        """Extract a human-readable reason from the underlying exception.

        Args:
            cause: Underlying exception.

        Returns:
            Human-readable reason string.
        """
        reason = QAuthException.DEFAULT_ERR_MSG

        # Handle JWT-related exceptions (python-jwt library)
        # Order matters: specific subclasses before jwt.InvalidTokenError
        if isinstance(cause, jwt.ExpiredSignatureError):
            reason = "JWT token has expired, please obtain a new LEXIS token"
        elif isinstance(cause, jwt.InvalidSignatureError):
            reason = "JWT token has invalid signature"
        elif isinstance(cause, jwt.InvalidAudienceError):
            reason = "JWT token has invalid audience"
        elif isinstance(cause, jwt.InvalidIssuerError):
            reason = "JWT token has invalid issuer"
        elif isinstance(cause, jwt.InvalidAlgorithmError):
            reason = f"JWT token uses invalid/unsupported algorithm: {cause!s}"
        elif isinstance(cause, jwt.InvalidKeyError):
            reason = f"JWT validation failed due to invalid key: {cause!s}"
        elif isinstance(cause, jwt.MissingRequiredClaimError):
            reason = f"JWT token missing required claim: {cause!s}"
        elif isinstance(cause, jwt.DecodeError):
            reason = f"JWT token decode error: {cause!s}"
        elif isinstance(cause, jwt.InvalidTokenError):
            reason = f"JWT token validation failed: {cause!s}"

        # Handle requests-related exceptions
        elif isinstance(cause, requests.RequestException):
            if getattr(cause, "response", None) is not None:
                if cause.response.status_code == 401:
                    reason = "Token is not authorized to access UserOrg API"
                elif cause.response.status_code == 403:
                    reason = (
                        "Insufficient permissions to access user project information"
                    )
                elif cause.response.status_code == 404:
                    reason = "User or project information not found in UserOrg service"
                else:
                    reason = f"UserOrg API error ({cause.response.status_code}): {cause.response.text}"
            else:
                reason = f"Failed to connect to UserOrg API: {cause!s}"

        # Anything else (e.g. QException from the aiohttp client): keep
        else:
            reason = f"{reason}: {cause!s}"

        return reason


class QResultsFailed(QException):
    """Exception raised when a HEAppE job fails in the background.

    Args:
        heappe_job_id: ID of the HEAppE job.
        message: Optional human-readable error message.
        **context: Optional context info (e.g., user_id, resource).
    """

    def __init__(self, heappe_job_id: int, message=None, **context):
        msg = f"HEAppE job '{heappe_job_id}' in background failed. "
        if message:
            msg += str(message)
        super().__init__(msg, **context)


class QPullaException(QException):
    """Base exception for QPulla-related errors.

    Args:
        message: Optional human-readable error message.
        **context: Optional context info (e.g., user_id, resource).
    """

    def __init__(self, message=None, **context):
        super().__init__(message, **context)


class QPullaFetchError(QPullaException):
    """Exception raised when there is an error fetching data from QPulla.

    Args:
        message: Optional human-readable error message.
        **context: Optional context info (e.g., user_id, resource).
    """

    def __init__(self, message=None, **context):
        super().__init__(message, **context)


#############
# Utilities #
#############


class HEAppEJobState(Enum):
    """Enum representing the states of a HEAppE job.

    Attributes:
        Configuring: Job is being configured.
        Submitted: Job has been submitted.
        Queued: Job is in the queue.
        Running: Job is running.
        Finished: Job has finished successfully.
        Failed: Job has failed.
        Canceled: Job has been canceled.
        Cancelled: Alias for Canceled.
        WaitingForServiceAccount: Job is waiting for a service account.
    """

    Configuring: int = 1
    Submitted: int = 2
    Queued: int = 4
    Running: int = 8
    Finished: int = 16
    Failed: int = 32
    Canceled: int = 64
    Cancelled: int = 64
    WaitingForServiceAccount: int = 128

    @classmethod
    def readable(cls, state):
        """Return a human-readable string representation of the job state.

        Args:
            state: Integer representing the job state.

        Returns:
            Human-readable string representation of the job state.
        """
        if any(state == x.value for x in cls):
            return HEAppEJobState(state).name.upper()
        else:
            return f"Unknown state {state}"


def is_valid_uuid(uuid_to_test, version=4):
    """Check if `uuid_to_test` is a valid UUID.

    Args:
        uuid_to_test: String to test.
        version: UUID version (1, 2, 3, or 4).

    Returns:
        True if `uuid_to_test` is a valid UUID, otherwise False.
    """
    try:
        if isinstance(uuid_to_test, UUID):
            return True
        uuid_obj = UUID(uuid_to_test, version=version)
    except ValueError:
        return False
    return str(uuid_obj) == uuid_to_test


#########
# PULLA #
#########


def qiskit_to_pulla(pulla, pulla_backend, qiskit_circuits):
    """Deprecated alias; delegates to iqm.pulla.utils_qiskit.qiskit_to_pulla.

    Args:
        pulla: Pulla instance.
        pulla_backend: Pulla backend.
        qiskit_circuits: Qiskit circuits.

    Returns:
        Result of the conversion.
    """
    return _iqm_qiskit_to_pulla(pulla, pulla_backend, qiskit_circuits)


def sweep_job_to_qiskit(
    job: PullaJob,
    *,
    shots: int,
) -> Result:
    """Convert a completed Pulla job to a Qiskit Result.

    This function is a patch for iqm.pulla.utils_qiskit.sweep_job_to_qiskit due to a bug with datetime import.

    Args:
        job: The completed job to convert.
        shots: Number of shots that was requested. Only used for validating the result.

    Returns:
        The equivalent Qiskit Result.

    Raises:
        ValueError: If the job result or circuit measurement results are missing.
    """
    circuit_execution_results = job.result()
    if circuit_execution_results is None:
        raise ValueError(
            f'Cannot format Qiskit result without result measurements. Job status is "{job.status.upper()}"'
        )

    if circuit_execution_results.circuit_measurement_results is None:
        raise ValueError("Cannot format station control result without result.")

    used_heralding = (
        sum(HERALDING_KEY in key for key in circuit_execution_results.sweep_results) > 0
    )

    # Convert the measurement results from a batch of circuits into the Qiskit format.
    batch_results: list[tuple[str, list[str]]] = [
        # TODO: Proper naming instead of "index"
        (
            f"{index:04d}",
            IQMJob._iqm_format_measurement_results(
                circuit_measurements,
                requested_shots=shots,
                expect_exact_shots=used_heralding,
            ),
        )
        for index, circuit_measurements in enumerate(
            circuit_execution_results.circuit_measurement_results
        )
    ]

    result_dict = {
        "backend_name": "IQMPullaBackend",
        "backend_version": "",
        "qobj_id": "",
        "job_id": str(job.job_id),
        "success": job.status == JobStatus.COMPLETED,
        "date": datetime.datetime.now(tz=datetime.UTC).date().isoformat(),
        "status": str(job.status),
        "timeline": job.data.timeline.copy(),
        "results": [
            {
                "shots": len(measurement_results),
                "success": True,
                "data": {
                    "memory": measurement_results,
                    "counts": Counts(Counter(measurement_results)),
                    "metadata": {},
                },
                "header": {"name": name},
            }
            for name, measurement_results in batch_results
        ],
    }
    return Result.from_dict(result_dict)