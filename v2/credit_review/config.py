"""Deployment settings for the public and pilot surfaces."""
import os


def public_demo():
    return os.getenv("VERCEL") == "1" or os.getenv("CREDIT_PUBLIC_DEMO") == "1"


def developer_api():
    configured = os.getenv("CREDIT_DEVELOPER_API")
    if configured is not None:
        return configured == "1"
    return not public_demo()
