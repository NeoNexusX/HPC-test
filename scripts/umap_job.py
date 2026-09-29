"""MassFlow UMAP on an OSS Zarr dataset, ported from the Function Compute handler.

Kept from FC: listing and path checks, skipping ion_image/intensity chunks, parallel conditional
GETs with size/ETag checks, the sampling budget and the snapshot diff that finds the files
UMAP added to the Zarr. Dropped: NAS fallback, resume manifests and time budgets, because every
INSTANT job starts on a fresh machine whose system disk and run time are set per job.
"""
from __future__ import annotations

from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
import errno
import json
import logging
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import threading

import oss2

logger = logging.getLogger(__name__)

TRANSFER_WORKERS = 8
TRANSFER_BATCH = 64
DEFAULT_DOWNLOAD_WORKERS = 32
LIST_WORKERS = 16
# Free space kept on the work disk beyond the download, for UMAP output and temporary files.
DISK_RESERVE_MIB = 1024
TRANSFORM_BATCH_SIZE = 1024
# ion_image/intensity is an ion-major copy of spectra/intensity. UMAP reads only spectra and axes,
# but the reader validates ion_image metadata and its offsets, so only this array's chunks are skipped.
ION_IMAGE_INTENSITY = "ion_image/intensity"
ZARR_METADATA_FILES = frozenset({"zarr.json", ".zarray", ".zattrs", ".zgroup", ".zmetadata"})
SETTINGS_FIELDS = {"oss_bucket", "oss_region", "oss_endpoint", "oss_prefix", "source_zarr_path",
                   "write_back", "work_dir", "skip_ion_image_chunks", "full_matrix_mib",
                   "sample_matrix_mib", "max_fit_samples"}
# Optional, echoed verbatim into result.json so a benchmark file records which planned
# configuration (instance type, cores, disk, batch, repeat) produced it. The container never
# acts on these values; the batch runner fills them in.
OPTIONAL_SETTINGS_FIELDS = {"benchmark_labels", "download_workers"}


def validate_settings(settings: dict, runtime: bool = False) -> None:
    required = SETTINGS_FIELDS | ({"run_id"} if runtime else set())
    extra = set(settings) - required
    if (required - set(settings)) or (extra - OPTIONAL_SETTINGS_FIELDS):
        raise ValueError("umap fields must match instant.example.json exactly")
    labels = settings.get("benchmark_labels")
    workers = settings.get("download_workers", DEFAULT_DOWNLOAD_WORKERS)
    if isinstance(workers, bool) or not isinstance(workers, int) or not 1 <= workers <= 128:
        raise ValueError("download_workers must be an integer from 1 to 128")
    if labels is not None:
        if not isinstance(labels, dict) or len(labels) > 20:
            raise ValueError("benchmark_labels must be a small mapping")
        for key, value in labels.items():
            if (not isinstance(key, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", key)
                    or isinstance(value, bool) or not isinstance(value, (str, int, float))
                    or (isinstance(value, str) and (len(value) > 256 or any(c in value for c in "\n\r")))):
                raise ValueError("benchmark_labels keys/values must be short strings or numbers")
    for key in ("oss_bucket", "oss_region", "oss_prefix", "source_zarr_path", "work_dir"):
        value = settings[key]
        if not isinstance(value, str) or not value or any(c in value for c in "<>\n\r"):
            raise ValueError(f"invalid umap.{key}")
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{1,61}[a-z0-9]", settings["oss_bucket"]):
        raise ValueError("invalid OSS bucket name")
    if not re.fullmatch(r"https://[a-zA-Z0-9.-]+", settings["oss_endpoint"]):
        raise ValueError("oss_endpoint must be an HTTPS service endpoint")
    if not re.fullmatch(r"[A-Za-z0-9_-]+(?:/[A-Za-z0-9_-]+)*", settings["oss_prefix"]):
        raise ValueError("oss_prefix must be a plain path without wildcards or traversal")
    source = settings["source_zarr_path"]
    if (not re.fullmatch(r"[A-Za-z0-9._-]+(?:/[A-Za-z0-9._-]+)*\.zarr", source)
            or any(part in {".", ".."} for part in source.split("/"))):
        raise ValueError("source_zarr_path must be an OSS key prefix ending in .zarr, "
                         "e.g. ehpc-benchmark/test_data/sample.zarr")
    path = Path(settings["work_dir"])
    if not path.is_absolute() or str(path).startswith("/dev") or path == Path("/") or ".." in path.parts:
        raise ValueError("work_dir must be an absolute dedicated directory, never a device or root")
    for key in ("write_back", "skip_ion_image_chunks"):
        if not isinstance(settings[key], bool):
            raise ValueError(f"{key} must be boolean")
    for key in ("full_matrix_mib", "sample_matrix_mib", "max_fit_samples"):
        value = settings[key]
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ValueError(f"{key} must be a positive integer")
    if runtime and not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", settings["run_id"]):
        raise ValueError("invalid run_id")


# ---------------------------------------------------------------------------
# CPU inventory, so each run records the machine it measured
# ---------------------------------------------------------------------------

def command(argv: list[str], timeout: int = 30) -> dict:
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        return {"argv": argv, "returncode": proc.returncode, "stdout": proc.stdout,
                "stderr": proc.stderr, "pass": proc.returncode == 0}
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"argv": argv, "pass": False, "error": type(exc).__name__}


LSCPU_FIELDS = {"Model name": "model", "Vendor ID": "vendor", "Architecture": "architecture",
                "CPU(s)": "cpus", "Socket(s)": "sockets", "Core(s) per socket": "cores_per_socket",
                "Thread(s) per core": "threads_per_core", "CPU max MHz": "max_mhz",
                "L3": "l3_cache", "Hypervisor vendor": "hypervisor"}


def _flatten(entries: list):
    for entry in entries:
        yield entry
        yield from _flatten(entry.get("children", []))


def lscpu_summary(lscpu: dict) -> dict:
    # util-linux >= 2.38 nests fields under "children"; older versions are flat.
    summary = {}
    for entry in _flatten(lscpu.get("lscpu", [])):
        key = LSCPU_FIELDS.get(entry.get("field", "").rstrip(":"))
        if key:
            summary.setdefault(key, entry.get("data"))
    return summary


def cpu_inventory() -> dict:
    result = command(["lscpu", "--json"])
    if result["pass"]:
        result["lscpu"] = json.loads(result.pop("stdout"))
        result["summary"] = lscpu_summary(result["lscpu"])
    result["os_cpu_count"] = os.cpu_count()
    result["affinity_cpu_ids"] = sorted(os.sched_getaffinity(0)) if hasattr(os, "sched_getaffinity") else None
    result["cgroup"] = {}
    for name in ("cpu.max", "memory.max", "cpu/cpu.cfs_quota_us", "memory/memory.limit_in_bytes"):
        path = Path("/sys/fs/cgroup") / name
        if path.is_file():
            result["cgroup"][name] = path.read_text().strip()
    return result


def cpu_lines(check: dict) -> list[str]:
    s = check.get("summary", {})
    return [f"cpu model: {s.get('model')} ({s.get('vendor')}, {s.get('architecture')})",
            f"cpu topology: cpus={s.get('cpus')} sockets={s.get('sockets')} "
            f"cores/socket={s.get('cores_per_socket')} threads/core={s.get('threads_per_core')} "
            f"max_mhz={s.get('max_mhz')} l3={s.get('l3_cache')} hypervisor={s.get('hypervisor')}",
            f"cpu visible to job: os_cpu_count={check.get('os_cpu_count')} cgroup={check.get('cgroup')}"]


# ---------------------------------------------------------------------------
# OSS listing and transfers
# ---------------------------------------------------------------------------

def error_name(exc: Exception) -> str:
    # Only the class and OSS error code: full SDK messages may embed signing details.
    code = getattr(exc, "code", None)
    name = type(exc).__name__
    return f"{name}:{code}" if isinstance(code, str) and code and code != name else name


def error_detail(exc: Exception) -> str:
    """Readable failure text; OSS SDK errors keep only class and code (see error_name)."""
    if type(exc).__module__.split(".")[0] == "oss2":
        return error_name(exc)
    return f"{type(exc).__name__}: {exc}"[:500]


def _object_record(obj, source: str) -> dict | None:
    if obj.key.endswith("/"):  # directory placeholder created by some upload tools
        return None
    rel = obj.key[len(source) + 1:]
    if any(part in {"", ".", ".."} for part in rel.split("/")) or "\\" in rel or "\x00" in rel:
        raise ValueError(f"object key cannot be mapped to a local file: {obj.key!r}")
    return {"key": obj.key, "size": obj.size, "etag": obj.etag.strip('"')}


def _list_prefix(bucket, source: str, prefix: str, delimiter: str = "") -> tuple[list[dict], list[str]]:
    objects, prefixes = [], []
    for obj in oss2.ObjectIteratorV2(bucket, prefix=prefix, delimiter=delimiter, max_keys=1000):
        if obj.key.endswith("/") and obj.size is None:
            prefixes.append(obj.key)
        else:
            record = _object_record(obj, source)
            if record is not None:
                objects.append(record)
    return objects, prefixes


def _list_exact(bucket, source: str, key: str) -> dict | None:
    # Prefix listing is used instead of HEAD because existing jobs only need List/Get permissions.
    for obj in oss2.ObjectIteratorV2(bucket, prefix=key, max_keys=1):
        if obj.key == key:
            return _object_record(obj, source)
    return None


def _list_numeric_chunks(bucket, source: str, prefix: str) -> list[dict]:
    """List a flat c/ chunk directory in parallel, including keys outside decimal prefixes.

    Decimal first characters are disjoint OSS prefixes. The two edge scans cover every other
    possible key, so this still enumerates all objects rather than assuming a particular layout.
    """
    def before_digits() -> list[dict]:
        objects = []
        for obj in oss2.ObjectIteratorV2(bucket, prefix=prefix, max_keys=1000):
            if obj.key >= f"{prefix}0":
                break
            record = _object_record(obj, source)
            if record is not None:
                objects.append(record)
        return objects

    def after_digits() -> list[dict]:
        objects = []
        for obj in oss2.ObjectIteratorV2(bucket, prefix=prefix, start_after=f"{prefix}:", max_keys=1000):
            record = _object_record(obj, source)
            if record is not None:
                objects.append(record)
        return objects

    with ThreadPoolExecutor(max_workers=13) as executor:
        futures = [executor.submit(_list_prefix, bucket, source, f"{prefix}{digit}")
                   for digit in "0123456789"]
        before = executor.submit(before_digits)
        colon = executor.submit(_list_exact, bucket, source, f"{prefix}:")
        after = executor.submit(after_digits)
        objects = [obj for future in futures for obj in future.result()[0]]
        objects.extend(before.result())
        if (record := colon.result()) is not None:
            objects.append(record)
        objects.extend(after.result())
    return objects


def _list_array(bucket, source: str, prefix: str) -> list[dict]:
    # The spectra array dominates max_2 (tens of thousands of chunks). Split its c/ keys so
    # one array does not turn the otherwise parallel listing back into a serial page chain.
    if prefix != f"{source}/spectra/intensity/":
        return _list_prefix(bucket, source, prefix)[0]
    direct, children = _list_prefix(bucket, source, prefix, delimiter="/")
    chunk_prefix = f"{prefix}c/"
    if chunk_prefix in children:
        children.remove(chunk_prefix)
        direct.extend(_list_numeric_chunks(bucket, source, chunk_prefix))
    for child in children:
        direct.extend(_list_prefix(bucket, source, child)[0])
    return direct


def list_source_objects(bucket, source: str, skip_ion_image_chunks: bool = False) -> tuple[list[dict], bool]:
    """Discover Zarr groups, then list their arrays concurrently.

    The boolean reports that chunk keys were never enumerated, so their count and size are unknown.
    """
    with ThreadPoolExecutor(max_workers=LIST_WORKERS) as executor:
        root_future = executor.submit(_list_prefix, bucket, source, f"{source}/", "/")
        spectra_keys = [f"{source}/spectra/intensity/{name}" for name in ("zarr.json", ".zarray")]
        spectra_futures = [executor.submit(_list_exact, bucket, source, key) for key in spectra_keys] \
            if skip_ion_image_chunks else []
        intensity = f"{source}/ion_image/intensity/"
        metadata_futures = [executor.submit(_list_exact, bucket, source, f"{intensity}{name}")
                            for name in sorted(ZARR_METADATA_FILES)] if skip_ion_image_chunks else []
        root_objects, groups = root_future.result()
        group_listings = list(executor.map(lambda prefix: _list_prefix(bucket, source, prefix, "/"), groups))
        direct_objects = [obj for group_objects, _ in group_listings for obj in group_objects]
        arrays = [prefix for _, prefixes in group_listings for prefix in prefixes]
        can_skip = skip_ion_image_chunks and any(future.result() for future in spectra_futures)
        excluded = can_skip and intensity in arrays
        if excluded:
            direct_objects.extend(obj for future in metadata_futures if (obj := future.result()) is not None)
            arrays.remove(intensity)
        array_listings = list(executor.map(lambda prefix: _list_array(bucket, source, prefix), arrays))
    objects = sorted(root_objects + direct_objects + [obj for group in array_listings for obj in group],
                     key=lambda obj: obj["key"])
    if not objects:
        raise FileNotFoundError(f"no objects under {source}/; check umap.source_zarr_path")
    return objects, excluded


def split_download_objects(objects: list[dict], source: str, skip_ion_image_chunks: bool) -> tuple[list, list]:
    """Return (to_download, skipped): only ion_image/intensity chunks are skipped, never metadata."""
    keys = {obj["key"] for obj in objects}
    has_spectra = any(f"{source}/spectra/intensity/{name}" in keys for name in ("zarr.json", ".zarray"))
    if not skip_ion_image_chunks or not has_spectra:
        return objects, []
    kept, skipped = [], []
    for obj in objects:
        rel = obj["key"][len(source) + 1:]
        chunk = rel.startswith(f"{ION_IMAGE_INTENSITY}/") and Path(rel).name not in ZARR_METADATA_FILES
        (skipped if chunk else kept).append(obj)
    return kept, skipped


def check_disk_space(directory: Path, download_bytes: int) -> None:
    free = shutil.disk_usage(directory).free
    needed = download_bytes + DISK_RESERVE_MIB * 2**20
    if free < needed:
        raise OSError(errno.ENOSPC, f"{directory} has {free / 2**20:.0f} MiB free but needs "
                                    f"{needed / 2**20:.0f} MiB; raise resources.system_disk_gib")


def _parallel(function, items: list) -> list:
    """Run in bounded batches on TRANSFER_WORKERS threads; the first failure stops later batches."""
    results = []
    with ThreadPoolExecutor(max_workers=TRANSFER_WORKERS) as executor:
        for start in range(0, len(items), TRANSFER_BATCH):
            results.extend(executor.map(function, items[start:start + TRANSFER_BATCH]))
    return results


def download_objects(bucket, objects: list[dict], source: str, destination: Path,
                     workers: int = DEFAULT_DOWNLOAD_WORKERS) -> int:
    """Download with If-Match on the listed ETag; a file appears only after its size and ETag match."""
    destination.mkdir(parents=True, exist_ok=True)
    # Partial files live outside the Zarr so they can never collide with a real object name.
    parts = Path(tempfile.mkdtemp(prefix="parts-", dir=destination.parent))
    stop = threading.Event()

    def download_one(obj: dict) -> int:
        if stop.is_set():
            return 0
        target = destination / obj["key"][len(source) + 1:]
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(dir=parts)
        os.close(fd)
        try:
            result = bucket.get_object_to_file(obj["key"], temporary, headers={"If-Match": f'"{obj["etag"]}"'})
            if os.path.getsize(temporary) != obj["size"] or result.etag != obj["etag"]:
                raise IOError(f"source changed during download: {obj['key']}")
            if not stop.is_set():
                os.replace(temporary, target)
            return obj["size"]
        finally:
            Path(temporary).unlink(missing_ok=True)

    try:
        total = 0
        with ThreadPoolExecutor(max_workers=workers) as executor:
            pending = {executor.submit(download_one, obj) for obj in objects[:workers]}
            remaining = iter(objects[workers:])
            while pending:
                done, pending = wait(pending, return_when=FIRST_COMPLETED)
                # Inspect all completed tasks before submitting more. An error stops the stream.
                failure = next((future.exception() for future in done if future.exception() is not None), None)
                if failure is not None:
                    stop.set()
                    for future in pending:
                        future.cancel()
                    raise failure
                total += sum(future.result() for future in done)
                for _ in done:
                    obj = next(remaining, None)
                    if obj is not None:
                        pending.add(executor.submit(download_one, obj))
        return total
    finally:
        shutil.rmtree(parts, ignore_errors=True)


def upload_files(bucket, root: Path, rel_paths: list[str], prefix: str) -> int:
    def upload_one(rel: str) -> int:
        bucket.put_object_from_file(f"{prefix}/{rel}", str(root / rel))
        return (root / rel).stat().st_size

    return sum(_parallel(upload_one, rel_paths))


def snapshot_dir(root: Path) -> dict:
    """{relative path: (size, mtime_ns)} for every file, the baseline for finding UMAP output."""
    snapshot = {}
    for path in root.rglob("*"):
        if path.is_file():
            st = path.stat()
            snapshot[path.relative_to(root).as_posix()] = (st.st_size, st.st_mtime_ns)
    return snapshot


def diff_snapshot(before: dict, after: dict) -> list[str]:
    """New or rewritten files. UMAP only adds analysis/umap, so deletions do not occur."""
    return sorted(rel for rel, meta in after.items() if before.get(rel) != meta)


# ---------------------------------------------------------------------------
# UMAP
# ---------------------------------------------------------------------------

def select_sample_ratio(dm, settings: dict) -> dict:
    """Decide the fit sample from the shared m/z axis that load_head_data read, not the full matrix."""
    if dm.storage_mode != "continuous":
        raise ValueError("UMAP requires continuous spectra with a shared m/z axis")
    n_pixels = len(dm.ms)
    n_features = len(dm.ms.shared_mz_list)
    if n_pixels < 5 or n_features == 0:
        raise ValueError("3-D UMAP requires at least 5 spectra and a non-empty m/z axis")
    row_bytes = 4 * n_features  # float32
    if n_pixels * row_bytes <= settings["full_matrix_mib"] * 2**20:
        sample_count = n_pixels
    else:
        sample_limit = min(settings["max_fit_samples"], settings["sample_matrix_mib"] * 2**20 // row_bytes)
        if sample_limit < 5:
            raise ValueError("UMAP sample memory budget cannot fit 5 spectra")
        sample_count = min(n_pixels, sample_limit)
    ratio = sample_count / n_pixels
    # MassFlow samples ceil(N * ratio) rows; keep float rounding from adding one row over budget.
    if sample_count < n_pixels and math.ceil(n_pixels * ratio) > sample_count:
        ratio = math.nextafter(ratio, 0.0)
    return {"pixels": n_pixels, "features": n_features, "matrix_mib": n_pixels * row_bytes / 2**20,
            "fit_samples": sample_count, "sample_ratio": ratio}


def import_massflow() -> None:
    """Import MassFlow and umap-learn; loads cached numba code, so it is timed as its own stage."""
    import massflow.data_manager  # noqa: F401
    import massflow.segmentation  # noqa: F401


def run_umap(settings: dict, local_source: Path, image_path: Path) -> dict:
    """Write analysis/umap into the local Zarr copy and save the RGB image, as the FC handler did."""
    from massflow.data_manager import MSDataManagerZarr
    from massflow.segmentation import plot_umap_image

    dm = MSDataManagerZarr(filepath=str(local_source))
    try:
        dm.load_head_data()
        info = select_sample_ratio(dm, settings)
        plot_umap_image(dm, save_matrix="zarr", sample_ratio=info["sample_ratio"],
                        transform_batch_size=TRANSFORM_BATCH_SIZE, output_path=str(image_path))
    finally:
        dm.close()
    return info


def write_synthetic_zarr(path: Path) -> Path:
    """A small MassFlow MSI Zarr with the test data's layout: ion_image plus spectra, float32."""
    import numpy as np
    from massflow.msi_zarr.writer import MSIZarrWriter

    rng = np.random.default_rng(0)
    width, height, mz = 12, 10, np.linspace(100, 500, 40)
    writer = MSIZarrWriter(str(path), row_axis="ion", encoding="continuous", metadata=None,
                           width=width, height=height, include_spectra=True)
    for y in range(1, height + 1):
        for x in range(1, width + 1):
            # Two spatial regions with different spectra give UMAP some structure.
            writer.add_spectrum(rng.random(mz.size, dtype=np.float32) + (x > width // 2), (x, y, 1), mz)
    writer.finalize()
    writer.close()
    return path


def warm_up() -> None:
    """Run UMAP once at image build so numba's cache and matplotlib's font cache ship in the image."""
    with tempfile.TemporaryDirectory() as directory:
        source = write_synthetic_zarr(Path(directory) / "warmup.zarr")
        budget = {"full_matrix_mib": 1024, "sample_matrix_mib": 1024, "max_fit_samples": 20000}
        run_umap(budget, source, Path(directory) / "warmup.jpg")
