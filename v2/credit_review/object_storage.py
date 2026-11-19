"""S3 access for explicitly enabled team intake."""
import os
from functools import lru_cache
from urllib.parse import urlsplit

from .documents import MAX_BYTES
from .intake import CSV_LIMIT


def enabled():
    return bool(os.getenv("CREDIT_UPLOAD_BUCKET") and os.getenv("CREDIT_UPLOAD_ORIGIN"))


def max_size(media_type):
    return CSV_LIMIT if media_type == "text/csv" else MAX_BYTES


@lru_cache(maxsize=1)
def client():
    import boto3
    from botocore.config import Config
    return boto3.client("s3", config=Config(signature_version="s3v4", connect_timeout=5, read_timeout=15,
                                           retries={"max_attempts": 2}))


def authorize(key, media_type, checksum):
    fields = {"Content-Type": media_type, "x-amz-meta-sha256": checksum}
    target = client().generate_presigned_post(
        Bucket=os.environ["CREDIT_UPLOAD_BUCKET"], Key=key, Fields=fields,
        Conditions=[{"Content-Type": media_type}, {"x-amz-meta-sha256": checksum},
                    ["content-length-range", 1, max_size(media_type)]], ExpiresIn=300,
    )
    url = urlsplit(target["url"])
    if url.scheme != "https" or f"{url.scheme}://{url.netloc}" != os.environ["CREDIT_UPLOAD_ORIGIN"].rstrip("/"):
        raise ValueError("The signed upload origin differs from the configured origin")
    return target


def describe(key):
    return client().head_object(Bucket=os.environ["CREDIT_UPLOAD_BUCKET"], Key=key)


def read_version(key, version, limit):
    obj = client().get_object(Bucket=os.environ["CREDIT_UPLOAD_BUCKET"], Key=key, VersionId=version)
    with obj["Body"] as data:
        payload = data.read(limit + 1)
    if len(payload) > limit:
        raise ValueError("Stored statement exceeds the permitted size")
    return payload
