"""Experimental fixed-capacity APFS workspace; no parent-death/lifecycle guarantee.

The host owns the image and mount. Seats only receive directories inside it;
neither the backing image nor disk-device access belongs in their Seatbelt roots.
The limit bounds allocated filesystem storage, not sparse-file logical lengths.
"""
from __future__ import annotations

from pathlib import Path
import os
import plistlib
import re
import shutil
import subprocess
import sys
import tempfile
import time

HDIUTIL = '/usr/bin/hdiutil'
DISKUTIL = '/usr/sbin/diskutil'


class StorageError(RuntimeError):
    pass


def _run(argv: list[str]) -> bytes:
    result = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            timeout=120, check=False)
    if result.returncode:
        raise StorageError(f'{Path(argv[0]).name} failed: ' +
                           (result.stdout + result.stderr).decode(errors='replace')[-2000:])
    return result.stdout


class Workspace:
    """Host-created disposable volume, with a shared allocation budget for all writes.

    Paths are usable only while entered. Cleanup never deletes a backing image
    until hdiutil confirms it is detached. Failed cleanup retains the private
    directory for recovery; it must not be interpreted as completed lifecycle
    containment or reliable cleanup after supervisor death.
    """
    def __init__(self, size_mib: int = 512):
        if type(size_mib) is not int or not 64 <= size_mib <= 16384:
            raise ValueError('workspace size must be 64..16384 MiB')
        self.size_bytes = size_mib * 1024 * 1024
        self.size_mib = size_mib
        self.root: Path | None = None
        self.active = False
        self.retained_reason = ''

    def retain(self, reason: str):
        """Preserve an owned image when its execution owner cannot confirm cleanup."""
        self.active = False
        self.retained_reason = reason or 'execution cleanup could not be verified'

    def __enter__(self):
        if self.root is not None:
            raise StorageError('workspace objects cannot be reused')
        if sys.platform != 'darwin' or not all(Path(p).is_file() for p in (HDIUTIL, DISKUTIL)):
            raise StorageError('native bounded storage requires macOS disk-image tools')
        self.root = Path(tempfile.mkdtemp(prefix='dk-volume-', dir='/tmp')).resolve()
        self.image = self.root / 'turn.dmg'
        self.mount = self.root / 'mount'
        self.mount.mkdir(mode=0o700)
        try:
            _run([HDIUTIL, 'create', '-size', f'{self.size_mib}m', '-fs', 'APFS',
                  '-fsargs', '-e',
                  '-type', 'UDIF', '-layout', 'GPTSPUD', '-volname', 'Diaktoros',
                  str(self.image)])
            self.image.chmod(0o600)
            image_info = plistlib.loads(_run([HDIUTIL, 'imageinfo', '-plist', str(self.image)]))
            if image_info.get('Format') != 'UDRW':
                raise StorageError('blank image is not fixed-size UDRW')
            _run([HDIUTIL, 'attach', '-plist', '-nobrowse', '-noautoopen',
                  '-owners', 'on', '-mountpoint', str(self.mount), str(self.image)])
            self._verify_mount()
            # Git trees require distinct case-sensitive names, even on a default Mac host.
            case_paths = (self.mount / 'CaseProbe', self.mount / 'caseprobe')
            for path in case_paths:
                with path.open('x'):
                    pass
            for path in case_paths:
                path.unlink()
            self.home, self.work, self.scratch = (self.mount / n for n in ('home', 'work', 'scratch'))
            for path in (self.home, self.work, self.scratch):
                path.mkdir(mode=0o700)
            self.active = True
            return self
        except BaseException:
            self._cleanup()
            raise

    def _attachments(self) -> list[dict]:
        info = plistlib.loads(_run([HDIUTIL, 'info', '-plist']))
        return [image for image in info.get('images', [])
                if Path(image.get('image-path', '')).resolve() == self.image]

    def _devices(self) -> list[str]:
        """Discover current attachments by this exact private image, never a cached disk id."""
        devices = []
        for image in self._attachments():
            entities = image.get('system-entities', [])
            whole = [e.get('dev-entry', '') for e in entities
                     if re.fullmatch(r'/dev/disk[0-9]+', e.get('dev-entry', ''))]
            if not whole:
                raise StorageError('owned image has no identifiable whole disk; retained for recovery')
            devices.append(whole[0])
        return devices

    def _verify_mount(self):
        info = plistlib.loads(_run([DISKUTIL, 'info', '-plist', str(self.mount)]))
        attachments = self._attachments()
        owned_mount = any(e.get('dev-entry') == info.get('DeviceNode') and
                          e.get('mount-point') == str(self.mount)
                          for image in attachments for e in image.get('system-entities', []))
        if (info.get('FilesystemType', '').lower() != 'apfs' or
                Path(info.get('MountPoint', '')).resolve() != self.mount or
                self.mount.stat().st_dev == self.root.stat().st_dev or
                len(attachments) != 1 or not owned_mount):
            raise StorageError('workspace is not the expected attached APFS filesystem')
        stats = os.statvfs(self.mount)
        capacity = stats.f_blocks * stats.f_frsize
        if not 0 < capacity <= self.size_bytes:
            raise StorageError('filesystem capacity exceeds the configured bound')
        if self.image.stat().st_size > self.size_bytes + 1024 * 1024:
            raise StorageError('backing image exceeds fixed-size format overhead allowance')

    def validate(self, *, home: Path, work: Path, scratch: Path):
        if not self.active:
            raise StorageError('workspace must be mounted and active before launch')
        self._verify_mount()
        expected = (self.home, self.work, self.scratch)
        supplied = tuple(Path(p).resolve(strict=True) for p in (home, work, scratch))
        if supplied != expected or any(p.stat().st_dev != self.mount.stat().st_dev for p in supplied):
            raise StorageError('all writable turn roots must belong to the bounded workspace')

    def _cleanup(self):
        self.active = False
        if self.root is None:
            return
        if self.retained_reason:
            raise StorageError(f'workspace retained at {self.root}: {self.retained_reason}')
        try:
            self._detach_owned()
            if self._devices() or self.mount.stat().st_dev != self.root.stat().st_dev:
                raise StorageError('workspace remains attached')
        except BaseException as exc:
            raise StorageError(f'workspace cleanup failed; retained at {self.root}') from exc
        shutil.rmtree(self.root)

    def _detach_owned(self):
        """Retry busy teardown briefly, rediscovering image ownership before each eject.

        A failed eject may already have unmounted the filesystem. Never reuse its
        disk id without checking the exact image again, including before force.
        Retries do not establish that detached seat descendants have exited.
        """
        for attempt in range(3):
            try:
                for device in self._devices():
                    if device not in self._devices():
                        continue
                    try:
                        _run([HDIUTIL, 'detach', device])
                    except StorageError:
                        if device in self._devices():
                            _run([HDIUTIL, 'detach', '-force', device])
                if not self._devices():
                    return
                raise StorageError('workspace remains attached')
            except StorageError as exc:
                if 'Resource busy' not in str(exc) or attempt == 2:
                    raise
                time.sleep(0.25 * (attempt + 1))

    def __exit__(self, *_):
        self._cleanup()
