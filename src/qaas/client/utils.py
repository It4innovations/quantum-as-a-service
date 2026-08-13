import datetime
from collections import Counter
from enum import Enum

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
    Base exception for QVAS.

    All custom exceptions in the QVAS project should inherit from this class.
    Provides optional context info like user_id, resource, or additional data.
    """

    def __init__(self, message=None, **context):
        """
        :param message: str, optional human-readable error message
        :param context: dict, optional context info (e.g., user_id, resource)
        """
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
    """

    DEFAULT_ERR_MSG = "Authentication error"

    def __init__(self, reason=None, user_id=None, resource=None):
        """
        :param reason: str, optional explanation of the authorization failure
        :param user_id: optional ID of the user
        :param resource: optional resource the user tried to access
        """
        if isinstance(self.__cause__, QAuthException):
            self.reason = self.__cause__.reason
            self.user_id = self.__cause__.user_id
            self.resource = self.__cause__.resource
        else:
            self.reason = reason or QAuthException.DEFAULT_ERR_MSG
            self.user_id = user_id
            self.resource = resource

            # If no reason provided, try to extract from the chained exception
            if reason is None and self.__cause__ is not None:
                reason = self._extract_reason_from_cause(self.__cause__)

        msg = self.reason
        if user_id:
            msg += f" (user_id={user_id})"
        if resource:
            msg += f", resource={resource}"

        super().__init__(msg)

    def _extract_reason_from_cause(self, cause) -> str:
        reason = self.reason

        # Handle JWT-related exceptions (python-jwt library)
        if isinstance(cause, jwt.ExpiredSignatureError):
            reason = "JWT token has expired"
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
            if hasattr(cause, "response") and cause.response is not None:
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

        return reason


class QResultsFailed(QException):
    def __init__(self, heappe_job_id: int, message=None, **context):
        msg = f"HEAppE job '{heappe_job_id}'in background failed. "
        if message:
            msg += str(message)
        super().__init__(msg, **context)


class QPullaExceeption(QException):
    def __init__(self, message=None, **context):
        super().__init__(message, **context)


class QPullaFetchError(QPullaExceeption):
    def __init__(self, message=None, **context):
        super().__init__(message, **context)


#############
# Utilities #
#############


class JobState(Enum):
    Configuring: int = 1
    Submitted: int = 2
    Queued: int = 4
    Running: int = 8
    Finished: int = 16
    Failed: int = 32
    Canceled: int = 64
    WaitingForServiceAccount: int = 128

    @classmethod
    def readable(cls, state):
        if any(state == x.value for x in cls):
            return JobState(state).name
        else:
            return f"Unknown state {state}"


#########
# PULLA #
#########


def qiskit_to_pulla(pulla, pulla_backend, qiskit_circuits):
    """Deprecated alias; delegates to iqm.pulla.utils_qiskit.qiskit_to_pulla."""
    return _iqm_qiskit_to_pulla(pulla, pulla_backend, qiskit_circuits)


def sweep_job_to_qiskit(
    job: PullaJob,
    *,
    shots: int,
) -> Result:
    """Convert a completed Pulla job to a Qiskit Result. (Patching function from iqm.pulla.utils_qiskit; Bug with datetime import)

    Args:
        job: The completed job to convert.
        shots: Number of shots that was requested. Only used for validating the result.

    Returns:
        The equivalent Qiskit Result.

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
