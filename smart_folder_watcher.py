"""Rule-based file sorter that watches a directory for new files."""

from __future__ import annotations

import argparse
import logging
import os
import shutil
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

LOGGER = logging.getLogger("smart_folder_watcher")


@dataclass(frozen=True)
class Rule:
    name: str
    destination: Path
    extensions: tuple[str, ...] = ()
    min_size: int | None = None
    max_size: int | None = None
    filename_contains: tuple[str, ...] = ()
    pdf_keywords: tuple[str, ...] = ()
    exif_camera: tuple[str, ...] = ()


class ConfigurationError(ValueError):
    """Raised when the YAML configuration is invalid."""


def _string_list(value: Any, field: str, rule_name: str) -> tuple[str, ...]:
    if value is None:
        return ()
    values = [value] if isinstance(value, str) else value
    if not isinstance(values, list) or not all(isinstance(item, str) for item in values):
        raise ConfigurationError(f"Rule '{rule_name}': '{field}' must be a string or a list of strings")
    return tuple(item.casefold() for item in values)


def load_config(config_path: Path) -> tuple[Path, bool, list[Rule]]:
    """Load and validate watcher settings and sorting rules from YAML."""
    try:
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ConfigurationError(f"Cannot read configuration '{config_path}': {exc}") from exc
    except yaml.YAMLError as exc:
        raise ConfigurationError(f"Invalid YAML in '{config_path}': {exc}") from exc

    if not isinstance(raw, dict):
        raise ConfigurationError("The configuration must contain a YAML mapping")

    watch_value = raw.get("watch_dir")
    if not isinstance(watch_value, str) or not watch_value.strip():
        raise ConfigurationError("'watch_dir' must be a non-empty path")
    watch_dir = Path(os.path.expandvars(watch_value)).expanduser().resolve()
    recursive = raw.get("recursive", False)
    if not isinstance(recursive, bool):
        raise ConfigurationError("'recursive' must be true or false")

    entries = raw.get("rules")
    if not isinstance(entries, list):
        raise ConfigurationError("'rules' must be a list")

    rules: list[Rule] = []
    for index, entry in enumerate(entries, start=1):
        if not isinstance(entry, dict):
            raise ConfigurationError(f"Rule #{index} must be a mapping")
        name = entry.get("name", f"rule-{index}")
        destination_value = entry.get("destination")
        if not isinstance(name, str) or not isinstance(destination_value, str) or not destination_value.strip():
            raise ConfigurationError(f"Rule #{index} needs a string 'name' and a non-empty 'destination'")
        destination = Path(os.path.expandvars(destination_value)).expanduser()
        if not destination.is_absolute():
            destination = (config_path.parent / destination).resolve()

        extensions = _string_list(entry.get("extensions"), "extensions", name)
        extensions = tuple(ext if ext.startswith(".") else f".{ext}" for ext in extensions)
        min_size = entry.get("min_size")
        max_size = entry.get("max_size")
        for field, value in (("min_size", min_size), ("max_size", max_size)):
            if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 0):
                raise ConfigurationError(f"Rule '{name}': '{field}' must be a non-negative integer")
        if min_size is not None and max_size is not None and min_size > max_size:
            raise ConfigurationError(f"Rule '{name}': 'min_size' cannot be greater than 'max_size'")

        rules.append(Rule(
            name=name,
            destination=destination,
            extensions=extensions,
            min_size=min_size,
            max_size=max_size,
            filename_contains=_string_list(entry.get("filename_contains"), "filename_contains", name),
            pdf_keywords=_string_list(entry.get("pdf_keywords"), "pdf_keywords", name),
            exif_camera=_string_list(entry.get("exif_camera"), "exif_camera", name),
        ))
    return watch_dir, recursive, rules


def _pdf_text(path: Path) -> str:
    try:
        from pypdf import PdfReader
    except ImportError as exc:
        raise RuntimeError("PDF keyword rules require pypdf; install requirements.txt") from exc
    reader = PdfReader(str(path))
    return "\n".join(page.extract_text() or "" for page in reader.pages).casefold()


def _exif_camera(path: Path) -> str:
    try:
        from PIL import Image, ExifTags
    except ImportError as exc:
        raise RuntimeError("EXIF rules require Pillow; install requirements.txt") from exc
    names = {value: key for key, value in ExifTags.TAGS.items()}
    with Image.open(path) as image:
        exif = image.getexif()
        make = str(exif.get(names.get("Make", -1), ""))
        model = str(exif.get(names.get("Model", -1), ""))
    return f"{make} {model}".strip().casefold()


def rule_matches(path: Path, rule: Rule, size: int | None = None) -> bool:
    """Return whether a file meets every condition specified by a rule."""
    if not path.is_file():
        return False
    try:
        size = path.stat().st_size if size is None else size
        if rule.extensions and path.suffix.casefold() not in rule.extensions:
            return False
        if rule.min_size is not None and size < rule.min_size:
            return False
        if rule.max_size is not None and size > rule.max_size:
            return False
        filename = path.name.casefold()
        if rule.filename_contains and not all(token in filename for token in rule.filename_contains):
            return False
        if rule.pdf_keywords:
            if path.suffix.casefold() != ".pdf":
                return False
            text = _pdf_text(path)
            if not all(keyword in text for keyword in rule.pdf_keywords):
                return False
        if rule.exif_camera:
            camera = _exif_camera(path)
            if not all(token in camera for token in rule.exif_camera):
                return False
        return True
    except (OSError, RuntimeError) as exc:
        LOGGER.warning("Could not inspect %s for rule '%s': %s", path, rule.name, exc)
        return False


def unique_destination(destination: Path) -> Path:
    """Avoid overwriting existing files by appending a numeric suffix."""
    if not destination.exists():
        return destination
    for number in range(1, 100_000):
        candidate = destination.with_name(f"{destination.stem} ({number}){destination.suffix}")
        if not candidate.exists():
            return candidate
    raise FileExistsError(f"Could not find an unused name for {destination}")


def sort_file(path: Path, rules: list[Rule], dry_run: bool = False) -> Path | None:
    """Move a file to the destination of the first matching rule."""
    try:
        path = path.resolve(strict=True)
        if not path.is_file():
            return None
    except OSError:
        return None

    for rule in rules:
        if not rule_matches(path, rule):
            continue
        rule.destination.mkdir(parents=True, exist_ok=True)
        destination_dir = rule.destination.resolve()
        if path.parent == destination_dir:
            return None
        destination = unique_destination(destination_dir / path.name)
        if dry_run:
            LOGGER.info("Would move %s -> %s (rule: %s)", path, destination, rule.name)
            return destination
        try:
            shutil.move(str(path), str(destination))
        except OSError as exc:
            LOGGER.error("Could not move %s to %s: %s", path, destination, exc)
            return None
        LOGGER.info("Moved %s -> %s (rule: %s)", path, destination, rule.name)
        return destination
    LOGGER.debug("No rule matched %s", path)
    return None


class SortHandler(FileSystemEventHandler):
    """Debounce filesystem events and sort files after writes finish."""

    def __init__(self, rules: list[Rule], dry_run: bool = False, stable_for: float = 1.0,
                 stable_timeout: float = 30.0) -> None:
        self.rules = rules
        self.dry_run = dry_run
        self.stable_for = stable_for
        self.stable_timeout = stable_timeout
        self._lock = threading.Lock()
        self._timers: dict[Path, threading.Timer] = {}

    def on_created(self, event: FileSystemEvent) -> None:
        self._handle(event)

    def on_moved(self, event: FileSystemEvent) -> None:
        self._handle(event, getattr(event, "dest_path", None))

    def _handle(self, event: FileSystemEvent, path_value: str | None = None) -> None:
        if event.is_directory:
            return
        value = path_value or event.src_path
        path = Path(value)
        with self._lock:
            previous = self._timers.pop(path, None)
            if previous:
                previous.cancel()
            timer = threading.Timer(0.25, self._process_when_stable, args=(path,))
            timer.daemon = True
            self._timers[path] = timer
            timer.start()

    def _process_when_stable(self, path: Path) -> None:
        deadline = time.monotonic() + self.stable_timeout
        previous: tuple[int, int] | None = None
        stable_since: float | None = None
        while time.monotonic() < deadline:
            try:
                stat = path.stat()
                current = (stat.st_size, stat.st_mtime_ns)
            except OSError:
                return
            if current == previous:
                stable_since = time.monotonic() if stable_since is None else stable_since
                if time.monotonic() - stable_since >= self.stable_for:
                    sort_file(path, self.rules, self.dry_run)
                    return
            else:
                previous = current
                stable_since = None
            time.sleep(0.25)
        LOGGER.warning("File did not become stable in time; leaving it in place: %s", path)

    def cancel_pending(self) -> None:
        with self._lock:
            timers = list(self._timers.values())
            self._timers.clear()
        for timer in timers:
            timer.cancel()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Sort files in a watched folder using YAML rules.")
    parser.add_argument("--config", type=Path, default=Path("config.yaml"), help="Path to YAML configuration (default: config.yaml)")
    parser.add_argument("--dry-run", action="store_true", help="Log planned moves without moving files")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        config_path = args.config.expanduser().resolve()
        watch_dir, recursive, rules = load_config(config_path)
        if not watch_dir.is_dir():
            raise ConfigurationError(f"Watch directory does not exist or is not a directory: {watch_dir}")
        handler = SortHandler(rules, dry_run=args.dry_run)
        observer = Observer()
        observer.schedule(handler, str(watch_dir), recursive=recursive)
        observer.start()
        LOGGER.info("Watching %s (recursive=%s, dry_run=%s)", watch_dir, recursive, args.dry_run)
        try:
            for path in watch_dir.iterdir():
                if path.is_file():
                    sort_file(path, rules, args.dry_run)
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            LOGGER.info("Stopping watcher")
        finally:
            observer.stop()
            observer.join()
            handler.cancel_pending()
    except ConfigurationError as exc:
        LOGGER.error("%s", exc)
        return 2
    except OSError as exc:
        LOGGER.error("Could not start watcher: %s", exc)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
