"""Every path and safety switch the tool uses, resolved in one place.

Nothing else in the package is allowed a hardcoded path. The original scripts
each carried their own `DATA = "/home/navknight/gphotos-sync-data"` and
`DRIVE = "/mnt/photos"`, which is exactly why none of them could be shared or
run on a second machine.

Precedence, highest first: CLI flag, environment variable, config file,
built-in default. The config file is `$GPHOTOS_CONFIG`, else `./gphotos.toml`,
else `~/.config/gphotos/config.toml`; a missing file is not an error, because
the defaults alone are enough for `gphotos organize`.

`archive` defaults to empty on purpose. It names an existing curated library
that the incremental `plan`/`embed`/`verify` commands write into, and those
commands are the dangerous ones. A user who never sets it cannot reach them by
accident.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

CONFIG_ENV = "GPHOTOS_CONFIG"
DEFAULT_DATA = "~/.local/share/gphotos"
DEFAULT_OUTPUT = "./Output"
DEFAULT_CLIENT_SECRET = "~/.config/gphotos/client_secret.json"


def _path(value: str | None) -> Path | None:
    """Expand ~ and $VARS; empty or None means "not configured"."""
    if not value:
        return None
    return Path(os.path.expandvars(os.path.expanduser(str(value)))).resolve()


@dataclass
class Safety:
    verify_readback: bool = True
    require_mount_uuid: str = ""


@dataclass
class Google:
    client_secret_file: Path | None = None


@dataclass
class Config:
    data: Path = field(default_factory=lambda: _path(DEFAULT_DATA))
    output: Path = field(default_factory=lambda: _path(DEFAULT_OUTPUT))
    archive: Path | None = None
    safety: Safety = field(default_factory=Safety)
    google: Google = field(default_factory=Google)
    source: Path | None = None  # which config file, if any, was read

    # Subdirectories of the data dir. Named rather than string-joined at each
    # call site so a layout change is one edit, not forty.
    @property
    def manifests(self) -> Path:
        return self.data / "manifests"

    @property
    def ledgers(self) -> Path:
        return self.data / "ledgers"

    @property
    def derived(self) -> Path:
        return self.data / "derived"

    @property
    def tokens(self) -> Path:
        return self.data / "tokens"

    @property
    def logs(self) -> Path:
        return self.data / "logs"

    @property
    def sidecar_dir(self) -> Path:
        return self.data / "takeout_sidecars"

    def mkdirs(self) -> None:
        for d in (self.manifests, self.ledgers, self.derived, self.logs):
            d.mkdir(parents=True, exist_ok=True)
        self.tokens.mkdir(parents=True, exist_ok=True, mode=0o700)

    def require_archive(self) -> Path:
        """The incremental commands are unusable without one; say so clearly."""
        if self.archive is None:
            sys.exit(
                "no archive configured. This command edits an existing photo\n"
                "library in place; it needs to be told which one.\n"
                "Set paths.archive in your config, or $GPHOTOS_ARCHIVE."
            )
        return self.archive

    def check_mount(self) -> None:
        """Refuse to run when the archive's drive is not actually there.

        Two separate failures this catches. An unmounted path still exists as
        an empty directory, so a run against it reports every file missing and
        (worse) a copy INTO it fills the root filesystem. And a removable drive
        can come back on a different device node holding different data, which
        is what safety.require_mount_uuid guards -- it was a hardcoded UUID in
        the original scripts, so nobody else could run them at all.
        """
        archive = self.require_archive()
        mount = archive
        while not os.path.ismount(mount) and mount != mount.parent:
            mount = mount.parent
        if mount == Path(mount.root) and not os.path.ismount(archive):
            sys.exit(f"REFUSING: nothing is mounted at or above {archive}")

        want = self.safety.require_mount_uuid.strip()
        if not want:
            return
        try:
            got = subprocess.run(
                ["findmnt", "-n", "-o", "UUID", "--target", str(archive)],
                capture_output=True, text=True, check=False,
            ).stdout.strip()
        except OSError:
            sys.exit("REFUSING: safety.require_mount_uuid is set but findmnt is "
                     "not available to check it")
        if got != want:
            sys.exit(f"REFUSING: {archive} is on filesystem {got or '(unknown)'}, "
                     f"not the configured {want}")


def _find_config_file(explicit: str | None) -> Path | None:
    if explicit:
        p = _path(explicit)
        if p is None or not p.is_file():
            sys.exit(f"config file not found: {explicit}")
        return p
    env = os.environ.get(CONFIG_ENV)
    if env:
        p = _path(env)
        if p is None or not p.is_file():
            sys.exit(f"${CONFIG_ENV} points at a file that does not exist: {env}")
        return p
    for cand in (Path("gphotos.toml"), _path("~/.config/gphotos/config.toml")):
        if cand and cand.is_file():
            return cand.resolve()
    return None


def _as_bool(v) -> bool:
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in ("1", "true", "yes", "on")


def load(args=None) -> Config:
    """Build the Config. `args` is the parsed argparse namespace, or None."""
    get = lambda name: getattr(args, name, None) if args else None

    path = _find_config_file(get("config"))
    doc = {}
    if path is not None:
        with open(path, "rb") as fh:
            doc = tomllib.load(fh)

    paths = doc.get("paths", {})
    safety = doc.get("safety", {})
    google = doc.get("google", {})

    def pick(flag, env, table_value, default):
        for v in (get(flag), os.environ.get(env), table_value, default):
            if v not in (None, ""):
                return v
        return None

    cfg = Config(
        data=_path(pick("data", "GPHOTOS_DATA", paths.get("data"), DEFAULT_DATA)),
        output=_path(pick("output", "GPHOTOS_OUTPUT", paths.get("output"),
                          DEFAULT_OUTPUT)),
        archive=_path(pick("archive", "GPHOTOS_ARCHIVE", paths.get("archive"), "")),
        safety=Safety(
            verify_readback=_as_bool(pick(
                "verify_readback", "GPHOTOS_VERIFY_READBACK",
                safety.get("verify_readback"), True)),
            require_mount_uuid=str(pick(
                "require_mount_uuid", "GPHOTOS_REQUIRE_MOUNT_UUID",
                safety.get("require_mount_uuid"), "") or ""),
        ),
        google=Google(client_secret_file=_path(pick(
            "client_secret_file", "GPHOTOS_CLIENT_SECRET",
            google.get("client_secret_file"), DEFAULT_CLIENT_SECRET))),
        source=path,
    )
    # --no-verify-readback is the one flag that has to be able to turn a
    # default-on switch off, which the "first non-empty wins" rule above cannot
    # express on its own.
    if args is not None and getattr(args, "no_verify_readback", False):
        cfg.safety.verify_readback = False
    return cfg
