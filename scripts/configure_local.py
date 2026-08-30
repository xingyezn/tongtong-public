#!/usr/bin/env python3
"""Generate ignored runtime settings from private/local.yaml.

The public repository tracks safe examples only. This script writes the two
runtime-specific files needed by the firmware and backend without committing
deployment addresses, passwords, or API credentials.
"""

import argparse
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = ROOT / "private" / "local.yaml"
FIRMWARE_PRIVATE_DEFAULTS = ROOT / "firmware" / "sdkconfig.defaults.private"
BACKEND_CONFIG = ROOT / "backend" / "config.yaml"


def read_private_config(path):
    with path.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}
    if not isinstance(data, dict):
        raise ValueError("private configuration must be a YAML mapping")
    return data


def require_mapping(data, name):
    value = data.get(name)
    if not isinstance(value, dict):
        raise ValueError("missing mapping: {}".format(name))
    return value


def update_firmware_ota_url(ota_url):
    if not isinstance(ota_url, str) or not ota_url:
        raise ValueError("firmware.ota_url must be a non-empty string")
    # Keep the tracked sdkconfig.defaults redacted. ESP-IDF merges this ignored
    # file when invoked with SDKCONFIG_DEFAULTS as documented in the README.
    text = "# Generated from private/local.yaml. Do not commit.\n"
    text += 'CONFIG_OTA_URL="{}"\n'.format(ota_url.replace('"', '\\"'))
    with FIRMWARE_PRIVATE_DEFAULTS.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)


def write_backend_config(backend):
    for section in ("server", "dashscope", "audio", "devices", "logging", "vad", "dashboard"):
        require_mapping(backend, section)
    with BACKEND_CONFIG.open("w", encoding="utf-8", newline="\n") as handle:
        yaml.safe_dump(backend, handle, allow_unicode=True, sort_keys=False)


def main():
    parser = argparse.ArgumentParser(description="Apply ignored private configuration")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    args = parser.parse_args()

    config_path = args.config.resolve()
    if not config_path.is_file():
        raise SystemExit(
            "Missing {}. Copy private/local.example.yaml to that path first.".format(config_path)
        )

    data = read_private_config(config_path)
    firmware = require_mapping(data, "firmware")
    backend = require_mapping(data, "backend")
    update_firmware_ota_url(firmware.get("ota_url"))
    write_backend_config(backend)
    print("Generated firmware/sdkconfig.defaults.private and ignored backend/config.yaml")


if __name__ == "__main__":
    main()
