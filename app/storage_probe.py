"""Explicit canary-only Storage verification. Never use customer objects.

Default: read bucket metadata only. --write-canary uploads tiny synthetic
objects, checks signed/public downloads and anonymous denial, and deletes only
its own UUID paths. Run inside the server: never export its sealed API key.
"""
import argparse
import asyncio
import io
import json
from uuid import uuid4

import httpx
from PIL import Image

from .object_storage import SupabaseObjectStorage, ObjectStorageError
from .supabase_api import supabase_headers


async def probe(settings, *, write_canary=False):
    storage = SupabaseObjectStorage(settings)
    checks = await storage.checks()
    if not write_canary or not all(checks.values()):
        return checks
    identifier = uuid4().hex
    private_key = f"deliveries/release-probe-{identifier}/test.txt"
    image_key = f"images/release-probe-{identifier}.webp"
    private_body = b"toolbako release probe: synthetic, not a customer delivery"
    output = io.BytesIO()
    Image.new("RGB", (8, 8), "#6560df").save(output, "WEBP")
    image_body = output.getvalue()
    cleanup_ok = True
    try:
        delivery = await storage.upload(private_key, private_body)
        await storage.upload(image_key, image_body, image=True)
        signed = await storage.signed_delivery_url({**delivery, "name": "test.txt"}, f"release-probe-{identifier}")
        async with httpx.AsyncClient(timeout=20, follow_redirects=False) as client:
            public = await client.get(storage.image_url(image_key))
            authorized = await client.get(signed)
            anonymous = await client.get(storage.origin + "/storage/v1/object/" + settings.supabase_private_bucket + "/" + private_key,
                                         headers=supabase_headers(settings.supabase_anon_key))
            checks.update(public_image_read=public.status_code == 200 and public.content == image_body,
                          signed_private_read=authorized.status_code == 200 and authorized.content == private_body,
                          anonymous_private_denied=anonymous.status_code in {400, 401, 403, 404})
    except Exception:
        checks["canary_io"] = False
    finally:
        for image, key in ((False, private_key), (True, image_key)):
            try:
                # Only freshly generated canary paths; never list or empty buckets.
                await storage.remove_probe(key, image=image)
            except ObjectStorageError:
                cleanup_ok = False
        checks["canary_cleanup"] = cleanup_ok
    return checks


def main():
    parser = argparse.ArgumentParser(description="Storage metadata/canary check; not launch approval")
    parser.add_argument("--write-canary", action="store_true")
    args = parser.parse_args()
    from .config import settings
    try:
        checks = asyncio.run(probe(settings, write_canary=args.write_canary))
        passed = all(checks.values())
        print(json.dumps({"storage_probe_passed": passed, "checks": checks, "canary": args.write_canary}))
        return 0 if passed else 1
    except Exception:
        print(json.dumps({"storage_probe_passed": False, "error": "Storage verification failed. No secrets, signed URLs or customer file data were printed."}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
