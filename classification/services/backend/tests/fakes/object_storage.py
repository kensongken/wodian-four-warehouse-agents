from product_engine.domain.errors import ObjectValidationFailed


class FakeObjectStorage:
    def __init__(self) -> None:
        self.objects: dict[str, tuple[str, int, str]] = {}

    async def create_upload(self, object_key: str, mime_type: str, max_bytes: int, sha256=None):
        from product_engine.storage.protocol import PresignedUpload

        return PresignedUpload(
            object_key=object_key,
            url=f"https://upload.test/{object_key}",
            headers={
                "Content-Type": mime_type,
                **({"x-amz-meta-sha256": sha256} if sha256 is not None else {}),
            },
            expires_in_seconds=900,
        )

    async def inspect(self, object_key: str):
        from product_engine.storage.protocol import StoredObject

        try:
            mime_type, byte_size, sha256 = self.objects[object_key]
        except KeyError as error:
            raise ObjectValidationFailed("object_not_found") from error
        return StoredObject(object_key, mime_type, byte_size, sha256)

    def complete(
        self,
        object_key: str,
        mime_type: str,
        byte_size: int,
        sha256: str,
    ) -> None:
        self.objects[object_key] = (mime_type, byte_size, sha256)
