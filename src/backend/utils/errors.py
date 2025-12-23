"""
Centralized error handling utilities for Trinity backend.

SECURITY: This module ensures internal error details are not exposed in HTTP responses.
Errors are logged internally but only generic messages are returned to clients.
"""
import logging
import traceback
import uuid
from typing import Optional
from fastapi import HTTPException

# Configure logging
logger = logging.getLogger("trinity.errors")


def log_and_raise(
    status_code: int,
    message: str,
    exception: Optional[Exception] = None,
    error_code: Optional[str] = None,
    include_trace: bool = True
) -> None:
    """
    Log the full error internally and raise an HTTPException with a safe message.

    Args:
        status_code: HTTP status code to return
        message: User-facing error message (should not contain internal details)
        exception: The original exception (logged but not exposed)
        error_code: Optional error reference code for support
        include_trace: Whether to log full traceback

    Usage:
        try:
            do_something()
        except Exception as e:
            log_and_raise(500, "Failed to process request", e)
    """
    # Generate error reference for correlation
    error_ref = error_code or f"ERR-{uuid.uuid4().hex[:8].upper()}"

    # Log full error details internally
    if exception:
        if include_trace:
            logger.error(
                f"[{error_ref}] {message}: {type(exception).__name__}: {str(exception)}\n"
                f"{traceback.format_exc()}"
            )
        else:
            logger.error(f"[{error_ref}] {message}: {type(exception).__name__}: {str(exception)}")
    else:
        logger.error(f"[{error_ref}] {message}")

    # Return safe error to client
    raise HTTPException(
        status_code=status_code,
        detail={
            "error": message,
            "reference": error_ref
        }
    )


def safe_error_message(error_type: str) -> str:
    """
    Return a safe, generic error message for common error types.

    Args:
        error_type: Type of error (e.g., "agent_creation", "authentication")

    Returns:
        Generic user-friendly error message
    """
    messages = {
        "agent_creation": "Failed to create agent. Please try again.",
        "agent_start": "Failed to start agent. Please try again.",
        "agent_stop": "Failed to stop agent. Please try again.",
        "agent_delete": "Failed to delete agent. Please try again.",
        "agent_communication": "Failed to communicate with agent. The agent may be starting up.",
        "authentication": "Authentication failed. Please log in again.",
        "authorization": "You don't have permission to perform this action.",
        "credential_access": "Failed to access credentials. Please try again.",
        "database": "A database error occurred. Please try again.",
        "configuration": "Configuration error. Please check your settings.",
        "network": "Network error occurred. Please try again.",
        "validation": "Invalid input provided. Please check your data.",
        "rate_limit": "Too many requests. Please wait and try again.",
        "not_found": "The requested resource was not found.",
        "conflict": "A conflict occurred. The resource may already exist.",
        "internal": "An internal error occurred. Please try again later.",
    }
    return messages.get(error_type, "An error occurred. Please try again.")


class SafeHTTPException(HTTPException):
    """
    HTTPException that automatically sanitizes error messages.

    Usage:
        raise SafeHTTPException(500, "agent_creation", original_exception)
    """

    def __init__(
        self,
        status_code: int,
        error_type: str,
        exception: Optional[Exception] = None,
        custom_message: Optional[str] = None
    ):
        error_ref = f"ERR-{uuid.uuid4().hex[:8].upper()}"

        # Log full error
        if exception:
            logger.error(
                f"[{error_ref}] {error_type}: {type(exception).__name__}: {str(exception)}\n"
                f"{traceback.format_exc()}"
            )

        # Use custom message or safe default
        message = custom_message or safe_error_message(error_type)

        super().__init__(
            status_code=status_code,
            detail={"error": message, "reference": error_ref}
        )
