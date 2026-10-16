"""Explicit canary-only Storage verification. Never use customer objects.

Default: read bucket metadata only. --write-canary uploads tiny synthetic
objects, checks signed/public downloads and anonymous denial, and deletes only
its own UUID paths. Run inside the server: never export its sealed API key.
"""
import argparse
import asyncio
import io
import json
import re
from uuid import uuid4

import httpx
from PIL import Image

from .object_storage import SupabaseObjectStorage, ObjectStorageError
from .supabase_api import supabase_headers


async def probe(settings, *, write_canary=False, phase="complete", canary_id=None, check_expiry=False):
    if phase not in {"complete", "prepare", "verify"} or (phase != "complete" and not write_canary):
        raise ValueError("Invalid canary phase")
    if canary_id is not None and not re.fullmatch(r"[a-f0-9]{32}", canary_id):
        raise ValueError("Invalid canary identifier")
    if phase == "verify" and not canary_id:
        raise ValueError("A prepared canary identifier is required")
    storage = SupabaseObjectStorage(settings)
    checks = await storage.checks()
    if not write_canary or not all(checks.values()):
        return checks
    identifier = canary_id or uuid4().hex
    private_key = f"deliveries/release-probe-{identifier}/test.txt"
    image_key = f"images/release-probe-{identifier}.webp"
    private_body = b"toolbako release probe: synthetic, not a customer delivery"
    output = io.BytesIO()
    Image.new("RGB", (8, 8), "#6560df").save(output, "WEBP")
    image_body = output.getvalue()
    cleanup_ok = True
    try:
        if phase != "verify":
            delivery = await storage.upload(private_key, private_body)
            await storage.upload(image_key, image_body, image=True)
        else:
            delivery = {"storage_backend": "supabase", "bucket": settings.supabase_private_bucket, "object_key": private_key}
        signed = await storage.signed_delivery_url({**delivery, "name": "test.txt"}, f"release-probe-{identifier}")
        async with httpx.AsyncClient(timeout=20, follow_redirects=False) as client:
            public = await client.get(storage.image_url(image_key))
            authorized = await client.get(signed)
            anonymous = await client.get(storage.origin + "/storage/v1/object/" + settings.supabase_private_bucket + "/" + private_key,
                                         headers=supabase_headers(settings.supabase_anon_key))
            checks.update(public_image_read=public.status_code == 200 and public.content == image_body,
                          signed_private_read=authorized.status_code == 200 and authorized.content == private_body,
                          anonymous_private_denied=anonymous.status_code in {400, 401, 403, 404})
            if check_expiry and checks["signed_private_read"]:
                await asyncio.sleep(65)
                expired = await client.get(signed)
                fresh_url = await storage.signed_delivery_url({**delivery, "name": "test.txt"}, f"release-probe-{identifier}")
                fresh = await client.get(fresh_url)
                checks["expired_signature_denied"] = expired.status_code in {400, 401, 403, 404}
                checks["object_read_after_expiry"] = fresh.status_code == 200 and fresh.content == private_body
    except Exception:
        checks["canary_io"] = False
    finally:
        if phase != "prepare" or not all(checks.values()):
            for image, key in ((False, private_key), (True, image_key)):
                try:
                    # Only synthetic UUID paths; never list or empty buckets.
                    await storage.remove_probe(key, image=image)
                except ObjectStorageError:
                    cleanup_ok = False
            checks["canary_cleanup"] = cleanup_ok
    return checks


def main():
    parser = argparse.ArgumentParser(description="Storage metadata/canary check; not launch approval")
    parser.add_argument("--write-canary", action="store_true")
    parser.add_argument("--phase", choices=("complete", "prepare", "verify"), default="complete")
    parser.add_argument("--canary-id")
    parser.add_argument("--check-expiry", action="store_true")
    args = parser.parse_args()
    from .config import settings
    try:
        identifier = args.canary_id or (uuid4().hex if args.phase == "prepare" else None)
        checks = asyncio.run(probe(settings, write_canary=args.write_canary, phase=args.phase,
                                  canary_id=identifier, check_expiry=args.check_expiry))
        passed = all(checks.values())
        print(json.dumps({"storage_probe_passed": passed, "checks": checks, "canary": args.write_canary,
                          "phase": args.phase, "canary_id": identifier}))
        return 0 if passed else 1
    except Exception:
        print(json.dumps({"storage_probe_passed": False, "error": "Storage verification failed. No secrets, signed URLs or customer file data were printed."}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
