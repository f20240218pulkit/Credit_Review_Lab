"""Production entry point for Vercel deployments from the repository root."""
from v2.credit_review.api import app

__all__ = ["app"]
