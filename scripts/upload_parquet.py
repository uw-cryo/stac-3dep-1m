#!/usr/bin/env python
"""
Upload a release's catalog.parquet to a public S3-compatible bucket.

Release assets on GitHub redirect, so browser tools like stac-map cannot read
them. This copies the published artifact to

    s3://<bucket>/<prefix>/<tag>.parquet

and, when <tag> is the latest release, to <prefix>/latest.parquet too, so there
is one stable URL that always tracks the newest catalog.

The parquet is the *release asset* by default, not a local build: what lands in
the bucket is byte-identical to what `releases/download/<tag>/catalog.parquet`
serves. Pass --file to upload a local build instead (it must still be named by
a --tag).

Credentials and endpoint come from the usual AWS variables, so locally:

    AWS_PROFILE=kopah-tacolab pixi run upload-parquet             # latest release
    AWS_PROFILE=kopah-tacolab pixi run upload-parquet --tag v2026.11.01
    pixi run upload-parquet --dry-run                             # no credentials needed

and in CI the `kopah` GitHub environment supplies AWS_ACCESS_KEY_ID,
AWS_SECRET_ACCESS_KEY and AWS_ENDPOINT_URL.
"""

import argparse
import json
import os
import shutil
import sys
import tempfile
import urllib.request
from pathlib import Path

REPO = "uw-cryo/stac-3dep-1m"
ASSET = "catalog.parquet"
# IANA-registered (2024); lets browsers and proxies treat it as binary data
CONTENT_TYPE = "application/vnd.apache.parquet"


def github_json(url):
    req = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json"})
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)


def latest_tag():
    return github_json(f"https://api.github.com/repos/{REPO}/releases/latest")[
        "tag_name"
    ]


def download_asset(tag, dest):
    url = f"https://github.com/{REPO}/releases/download/{tag}/{ASSET}"
    print(f"downloading {url}")
    with urllib.request.urlopen(url, timeout=300) as r, open(dest, "wb") as f:
        shutil.copyfileobj(r, f, length=8 << 20)


def s3_client(endpoint_url):
    import boto3
    from botocore.config import Config

    # Recent botocore sends CRC checksums by default, which Ceph RGW rejects;
    # set here too so the script does not depend on the profile carrying it
    cfg = Config(
        request_checksum_calculation="when_required",
        response_checksum_validation="when_required",
    )
    client = boto3.client("s3", endpoint_url=endpoint_url, config=cfg)
    host = client.meta.endpoint_url
    if "amazonaws.com" in host:
        sys.exit(
            f"refusing to upload to {host}: no S3 endpoint configured. "
            "Set AWS_PROFILE=kopah-tacolab (or AWS_ENDPOINT_URL), or pass --endpoint-url."
        )
    return client


def head(client, bucket, key):
    from botocore.exceptions import ClientError

    try:
        return client.head_object(Bucket=bucket, Key=key)
    except ClientError as e:
        if e.response["Error"]["Code"] in ("404", "NoSuchKey", "NotFound"):
            return None
        raise


def public_url(client, bucket, key):
    return f"{client.meta.endpoint_url.rstrip('/')}/{bucket}/{key}"


def main():
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--tag", help="release tag to upload (default: the latest release)")
    p.add_argument(
        "--file",
        type=Path,
        help=f"upload this local parquet instead of the release's {ASSET}",
    )
    p.add_argument("--bucket", default="tacolab")
    p.add_argument("--prefix", default="stac-3dep-1m")
    p.add_argument(
        "--endpoint-url",
        default=None,
        help="default: AWS_ENDPOINT_URL or the profile's endpoint_url",
    )
    p.add_argument(
        "--overwrite",
        action="store_true",
        help="replace <tag>.parquet if it already exists",
    )
    p.add_argument(
        "--no-latest", action="store_true", help="do not update latest.parquet"
    )
    p.add_argument(
        "--dry-run", action="store_true", help="print what would be uploaded and stop"
    )
    args = p.parse_args()

    newest = latest_tag()
    tag = args.tag or newest
    # Only the newest release may move latest.parquet: re-uploading an old tag
    # must not roll the stable URL back
    update_latest = not args.no_latest and tag == newest
    prefix = args.prefix.strip("/")
    key = f"{prefix}/{tag}.parquet"
    latest_key = f"{prefix}/latest.parquet"

    print(f"tag {tag} (latest release: {newest})")
    print(f"  -> s3://{args.bucket}/{key}")
    if update_latest:
        print(f"  -> s3://{args.bucket}/{latest_key}")
    if args.dry_run:
        return

    client = s3_client(args.endpoint_url)
    existing = head(client, args.bucket, key)

    if existing and not args.overwrite:
        print(
            f"s3://{args.bucket}/{key} already exists ({existing['ContentLength']} bytes); skipping upload (--overwrite to replace)"
        )
    else:
        with tempfile.TemporaryDirectory() as tmp:
            src = args.file
            if src is None:
                src = Path(tmp) / ASSET
                download_asset(tag, src)
            size = src.stat().st_size
            print(f"uploading {src} ({size} bytes)")
            client.upload_file(
                str(src),
                args.bucket,
                key,
                ExtraArgs={"ContentType": CONTENT_TYPE},
            )
            got = head(client, args.bucket, key)
            if got is None or got["ContentLength"] != size:
                sys.exit(
                    f"upload check failed: expected {size} bytes, bucket has {got and got['ContentLength']}"
                )

    if update_latest:
        client.copy_object(
            Bucket=args.bucket,
            Key=latest_key,
            CopySource={"Bucket": args.bucket, "Key": key},
            ContentType=CONTENT_TYPE,
            MetadataDirective="REPLACE",
            Metadata={"release-tag": tag},
        )

    print(public_url(client, args.bucket, key))
    if update_latest:
        print(public_url(client, args.bucket, latest_key))


if __name__ == "__main__":
    main()
