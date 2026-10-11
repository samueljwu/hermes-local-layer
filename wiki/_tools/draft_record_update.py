"""Descriptor-bound, draft-only record updates; no admission authority."""
import contextlib
import fcntl
import json
import os
import stat
import uuid
from pathlib import Path

LIMIT = 16 * 1024 * 1024
FIELDS = {"claims", "sources", "visuals", "annotations"}


@contextlib.contextmanager
def directory(path):
    """Bind the whole absolute hierarchy, refusing symlink ancestors."""
    path = Path(path).absolute()
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    fd = os.open(path.anchor, flags)
    try:
        for part in path.parts[1:]:
            next_fd = os.open(part, flags, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        yield fd
    finally:
        os.close(fd)


def read_at(fd, name, gate):
    handle = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
    with os.fdopen(handle, "rb") as source:
        info = os.fstat(source.fileno())
        gate.need(stat.S_ISREG(info.st_mode), f"not a regular input: {name}")
        gate.need(info.st_size <= LIMIT, f"input exceeds 16 MiB: {name}")
        data = source.read(LIMIT + 1)
        gate.need(len(data) <= LIMIT, f"input exceeds 16 MiB: {name}")
        return data


def replace_at(fd, name, data, expected, gate):
    """Publish in the verified directory even if its pathname is replaced."""
    temp = ".draft-" + uuid.uuid4().hex
    handle = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=fd)
    try:
        with os.fdopen(handle, "wb") as output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        gate.need(gate.sha(read_at(fd, name, gate)) == expected, "concurrent record change; refusing overwrite")
        os.replace(temp, name, src_dir_fd=fd, dst_dir_fd=fd)
        os.fsync(fd)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temp, dir_fd=fd)


def update(root, ingest_id, gate):
    ingest_id = gate.identifier(ingest_id)
    with directory(Path(root) / ".ingest-work" / ingest_id) as proposal_fd:
        proposal = json.loads(read_at(proposal_fd, "record-update.json", gate), object_pairs_hook=gate.unique_object)
    gate.need(isinstance(proposal, dict) and set(proposal) == {"expected_record_sha256", "updates"},
              "draft update requires only expected_record_sha256 and updates")
    expected = proposal["expected_record_sha256"]
    gate.need(gate.is_hash(expected), "invalid expected record hash")
    updates = proposal["updates"]
    gate.need(isinstance(updates, dict) and bool(updates) and set(updates) <= FIELDS,
              "draft updates may change only claims, sources, visuals and annotations")
    for key, value in updates.items():
        if key == "annotations":
            gate.need(isinstance(value, dict), "draft annotations must be an object")
        else:
            gate.need(isinstance(value, list) and all(isinstance(item, dict) for item in value),
                      f"draft field {key} must be a list of objects")
    with directory(Path(root) / "_meta/ingestion") as meta_fd:
        lock_fd = os.open("apply.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600, dir_fd=meta_fd)
        with os.fdopen(lock_fd, "rb") as lock:
            gate.need(stat.S_ISREG(os.fstat(lock.fileno()).st_mode), "not a regular ingestion lock")
            fcntl.flock(lock, fcntl.LOCK_EX)
            with directory(Path(root) / "_meta/ingestion/records") as record_fd:
                name = ingest_id + ".json"
                current = read_at(record_fd, name, gate)
                gate.need(gate.sha(current) == expected, "draft record changed; refresh proposal before retrying")
                record = json.loads(current, object_pairs_hook=gate.unique_object)
                gate.need(isinstance(record, dict) and record.get("id") == ingest_id, "record identity mismatch")
                gate.need(record.get("state") == "draft", "only draft records may be updated")
                gate.need(not any(key in record for key in ("payload_sha256", "review", "review_sha256", "applied_at")),
                          "draft contains sealed/review/publication fields; refusing update")
                record.update(updates)
                data = json.dumps(record, indent=2, ensure_ascii=False).encode() + b"\n"
                gate.need(len(data) <= LIMIT, "updated record exceeds 16 MiB")
                replace_at(record_fd, name, data, expected, gate)
    return {"ok": True, "id": ingest_id, "state": "draft", "updated_fields": sorted(updates),
            "record_sha256": gate.sha(data), "admission_review_required": True}
