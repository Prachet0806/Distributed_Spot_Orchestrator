"""Filesystem object service for the explicit lab; provides no KMS/IAM proof."""
from pathlib import Path
import json
import shutil
from io import BytesIO
from botocore.exceptions import ClientError


class LocalArtifactClient:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, bucket, key):
        path = (self.root / bucket / key).resolve()
        if not path.is_relative_to(self.root):
            raise ValueError("artifact path escapes the lab")
        return path

    def upload_file(self, source, bucket, key, ExtraArgs=None):
        target = self._path(bucket, key)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        target.with_name(target.name + ".metadata").write_text(json.dumps((ExtraArgs or {}).get("Metadata", {})))

    def put_object(self, *, Bucket, Key, Body, IfNoneMatch=None,
                   ServerSideEncryption=None, SSEKMSKeyId=None, Metadata=None):
        target = self._path(Bucket, Key)
        target.parent.mkdir(parents=True, exist_ok=True)
        if IfNoneMatch != "*":
            raise ValueError("local lab only supports create-only object writes")
        try:
            with target.open("xb") as output:
                output.write(Body)
        except FileExistsError as exc:
            raise ClientError({"Error": {"Code": "PreconditionFailed"}}, "PutObject") from exc
        target.with_name(target.name + ".metadata").write_text(json.dumps(Metadata or {}))
        return {"ETag": '"local"'}

    def head_object(self, Bucket, Key):
        path = self._path(Bucket, Key)
        if not path.exists():
            raise ClientError({"Error": {"Code": "404"}}, "HeadObject")
        return {"Metadata": json.loads(path.with_name(path.name + ".metadata").read_text()),
                "ContentLength": path.stat().st_size}

    def get_object(self, Bucket, Key):
        path = self._path(Bucket, Key)
        if not path.exists():
            raise ClientError({"Error": {"Code": "NoSuchKey"}}, "GetObject")
        return {"Body": BytesIO(path.read_bytes())}

    def download_file(self, bucket, key, destination, **kwargs):
        shutil.copyfile(self._path(bucket, key), destination)

    def delete_object(self, Bucket, Key):
        path = self._path(Bucket, Key)
        path.unlink(missing_ok=True)
        path.with_name(path.name + ".metadata").unlink(missing_ok=True)
