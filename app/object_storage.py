"""Server-only Supabase Storage. No secrets or signed URLs enter business state.

Product images are re-encoded WebP in a public CDN bucket. Paid deliveries use
a distinct private bucket; only participant-verified routes may sign them.
This module never creates buckets, changes permissions or retries uploads.
"""
import re
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

from .supabase_api import supabase_headers


class ObjectStorageError(RuntimeError):
    pass


def storage_origin(settings):
    try:
        parsed = urlparse(settings.supabase_url)
        valid = (parsed.scheme == "https" and re.fullmatch(r"[a-z0-9-]+\.supabase\.co", parsed.hostname or "")
                 and parsed.port in {None, 443} and not parsed.username and not parsed.password
                 and parsed.path in {"", "/"} and not parsed.query and not parsed.fragment)
    except ValueError:
        return ""
    if not valid: return ""
    return f"https://{parsed.hostname}"


def storage_configured(settings):
    return (settings.storage_provider == "supabase" and bool(storage_origin(settings))
            and bool(settings.supabase_service_role_key)
            and not settings.supabase_service_role_key.startswith("sb_publishable_")
            and all(re.fullmatch(r"[a-z0-9][a-z0-9-]{2,62}", name or "")
                    for name in (settings.supabase_private_bucket, settings.supabase_image_bucket))
            and settings.supabase_private_bucket != settings.supabase_image_bucket)


def safe_object_key(key):
    if (not isinstance(key, str) or len(key) > 240 or not re.fullmatch(r"[a-zA-Z0-9/_\-.]+", key)
            or any(part in {"", ".", ".."} for part in key.split("/"))):
        raise ObjectStorageError("保存するファイルの参照を確認できません")
    return key


async def storage_ready(settings):
    if not storage_configured(settings):
        return False
    return all((await SupabaseObjectStorage(settings).checks()).values())


class SupabaseObjectStorage:
    def __init__(self, settings):
        if not storage_configured(settings):
            raise ObjectStorageError("永続ファイル保存先の設定が必要です")
        self.settings = settings
        self.origin = storage_origin(settings)

    async def _request(self, method, path, *, json=None, content=None, headers=None, response_type=dict):
        try:
            async with httpx.AsyncClient(timeout=30, follow_redirects=False) as client:
                response = await client.request(method, self.origin + "/storage/v1/" + path,
                    headers={**supabase_headers(self.settings.supabase_service_role_key), **(headers or {})},
                    json=json, content=content)
            if not 200 <= response.status_code < 300:
                raise ValueError("provider unavailable")
            data = response.json()
            if not isinstance(data, response_type):
                raise ValueError("invalid object")
            return data
        except (httpx.HTTPError, ValueError):
            raise ObjectStorageError("永続ファイル保存サービスを確認できません。再送せず運営へお問い合わせください") from None

    async def remove_probe(self, key, *, image=False):
        """Only synthetic UUID canaries; never expose general deletion here."""
        pattern = r"images/release-probe-[a-f0-9]{32}\.webp" if image else r"deliveries/release-probe-[a-f0-9]{32}/test\.txt"
        if not isinstance(key, str) or not re.fullmatch(pattern, key):
            raise ObjectStorageError("削除対象は今回の検証用ファイルに限定されています")
        bucket = self.settings.supabase_image_bucket if image else self.settings.supabase_private_bucket
        result = await self._request("DELETE", f"object/{bucket}", json={"prefixes": [key]}, response_type=list)
        if len(result) > 1 or any(not isinstance(row, dict) or row.get("name") != key for row in result):
            raise ObjectStorageError("検証用ファイルの削除結果を確認できません")

    async def verify_bucket(self, *, image=False):
        bucket = self.settings.supabase_image_bucket if image else self.settings.supabase_private_bucket
        limit, mime = (5_000_000, "image/webp") if image else (50_000_000, "application/octet-stream")
        data = await self._request("GET", f"bucket/{bucket}")
        if (data.get("id") != bucket or data.get("name") != bucket or data.get("public") is not image
                or type(data.get("file_size_limit")) is not int or data["file_size_limit"] != limit
                or data.get("allowed_mime_types") != [mime]):
            raise ObjectStorageError("保存先の公開範囲・容量・形式が一致しません")
        return bucket

    async def checks(self):
        results = {}
        for image, key in ((False, "private_delivery_bucket"), (True, "public_image_bucket")):
            try:
                await self.verify_bucket(image=image)
                results[key] = True
            except ObjectStorageError:
                results[key] = False
        return results

    async def upload(self, key, content, *, image=False):
        safe_object_key(key)
        prefix, limit, mime = ("images/", 5_000_000, "image/webp") if image else ("deliveries/", 50_000_000, "application/octet-stream")
        if not key.startswith(prefix) or not isinstance(content, bytes) or not 0 < len(content) <= limit:
            raise ObjectStorageError("保存するファイルの種類・容量を確認できません")
        if image and not (content.startswith(b"RIFF") and content[8:12] == b"WEBP"):
            raise ObjectStorageError("商品画像は変換済みWebPだけ保存できます")
        bucket = await self.verify_bucket(image=image)
        result = await self._request("POST", f"object/{bucket}/{key}", content=content,
            headers={"Content-Type": mime, "cache-control": "max-age=31536000" if image else "max-age=0",
                     "x-upsert": "false"})
        if result.get("Key") != f"{bucket}/{key}":
            raise ObjectStorageError("保存結果のファイル参照が一致しません")
        return {"storage_backend": "supabase", "bucket": bucket, "object_key": key}

    def image_url(self, key):
        safe_object_key(key)
        if not key.startswith("images/"):
            raise ObjectStorageError("商品画像の参照が一致しません")
        return f"{self.origin}/storage/v1/object/public/{self.settings.supabase_image_bucket}/{key}"

    async def signed_delivery_url(self, record, order_id):
        bucket = await self.verify_bucket()
        key = safe_object_key(record.get("object_key"))
        safe_object_key(order_id)
        if (record.get("storage_backend") != "supabase" or record.get("bucket") != bucket
                or not key.startswith(f"deliveries/{order_id}/") or len(key.split("/")) != 3):
            raise ObjectStorageError("注文と納品ファイルの保存先が一致しません")
        data = await self._request("POST", f"object/sign/{bucket}/{key}", json={"expiresIn": 60})
        value = data.get("signedURL")
        if not isinstance(value, str):
            raise ObjectStorageError("期限付きダウンロードを確認できません")
        if value.startswith("/object/sign/"):
            value = self.origin + "/storage/v1" + value
        parsed = urlparse(value)
        query = parse_qs(parsed.query, keep_blank_values=True)
        if (parsed.scheme != "https" or parsed.netloc != urlparse(self.origin).netloc
                or parsed.path != f"/storage/v1/object/sign/{bucket}/{key}" or parsed.fragment
                or set(query) != {"token"} or len(query["token"]) != 1
                or not re.fullmatch(r"[A-Za-z0-9_.-]{1,8192}", query["token"][0])):
            raise ObjectStorageError("安全な期限付きダウンロード先を確認できません")
        name = record.get("name", "download")
        if not isinstance(name, str) or not 1 <= len(name) <= 120 or any(ord(c) < 32 for c in name):
            raise ObjectStorageError("ダウンロードするファイル名を確認できません")
        return self.origin + parsed.path + "?" + urlencode({"token": query["token"][0], "download": name})
