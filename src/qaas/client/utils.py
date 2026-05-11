from enum import Enum
import requests
import jwt
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
            reason = f"JWT token uses invalid/unsupported algorithm: {str(cause)}"
        elif isinstance(cause, jwt.InvalidKeyError):
            reason = f"JWT validation failed due to invalid key: {str(cause)}"
        elif isinstance(cause, jwt.MissingRequiredClaimError):
            reason = f"JWT token missing required claim: {str(cause)}"
        elif isinstance(cause, jwt.DecodeError):
            reason = f"JWT token decode error: {str(cause)}"
        elif isinstance(cause, jwt.InvalidTokenError):
            reason = f"JWT token validation failed: {str(cause)}"

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
                reason = f"Failed to connect to UserOrg API: {str(cause)}"

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
