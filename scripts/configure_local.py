#!/usr/bin/env python3
"""Generate ignored runtime settings from public.yaml and private/local.yaml.

The repository tracks safe team-wide endpoints in public.yaml. Local settings
override those defaults and retain passwords, credentials, and device tokens.
"""

import argparse
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PUBLIC_CONFIG = ROOT / "public.yaml"
DEFAULT_CONFIG = ROOT / "private" / "local.yaml"
FIRMWARE_PRIVATE_DEFAULTS = ROOT / "firmware" / "sdkconfig.defaults.private"
BACKEND_CONFIG = ROOT / "backend" / "config.yaml"


def read_config(path, label):
    with path.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}
    if not isinstance(data, dict):
        raise ValueError("{} configuration must be a YAML mapping".format(label))
    return data


def require_mapping(data, name):
    value = data.get(name)
    if not isinstance(value, dict):
        raise ValueError("missing mapping: {}".format(name))
    return value


def merge_mappings(base, overrides):
    """Return a deep merge where values in overrides take precedence."""
    result = dict(base)
    for key, value in overrides.items():
        base_value = result.get(key)
        if isinstance(base_value, dict) and isinstance(value, dict):
            result[key] = merge_mappings(base_value, value)
        else:
            result[key] = value
    return result


def validate_public_config(data):
    """Keep the tracked file limited to explicitly non-secret endpoints."""
    allowed = {
        "firmware": {"ota_url"},
        "backend": {"server": {"public_ws_url"}},
    }
    if set(data) - set(allowed):
        raise ValueError("public.yaml may contain only firmware and backend")
    firmware = require_mapping(data, "firmware")
    backend = require_mapping(data, "backend")
    server = require_mapping(backend, "server")
    if set(firmware) - allowed["firmware"]:
        raise ValueError("public.yaml firmware may contain only ota_url")
    if set(backend) - set(allowed["backend"]):
        raise ValueError("public.yaml backend may contain only server")
    if set(server) - allowed["backend"]["server"]:
        raise ValueError("public.yaml backend.server may contain only public_ws_url")
    for name, value in (("firmware.ota_url", firmware.get("ota_url")),
                        ("backend.server.public_ws_url", server.get("public_ws_url"))):
        if not isinstance(value, str) or not value:
            raise ValueError("{} must be a non-empty string".format(name))


def update_firmware_ota_url(ota_url):
    if not isinstance(ota_url, str) or not ota_url:
        raise ValueError("firmware.ota_url must be a non-empty string")
    # Keep the tracked sdkconfig.defaults redacted. ESP-IDF merges this ignored
    # file when invoked with SDKCONFIG_DEFAULTS as documented in the README.
    text = "# Generated from public.yaml and private/local.yaml. Do not commit.\n"
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
    parser.add_argument("--public-config", type=Path, default=DEFAULT_PUBLIC_CONFIG)
    args = parser.parse_args()

    config_path = args.config.resolve()
    public_config_path = args.public_config.resolve()
    if not public_config_path.is_file():
        raise SystemExit("Missing team configuration: {}".format(public_config_path))
    if not config_path.is_file():
        raise SystemExit(
            "Missing {}. Copy private/local.example.yaml to that path first.".format(config_path)
        )

    public_data = read_config(public_config_path, "public")
    validate_public_config(public_data)
    private_data = read_config(config_path, "private")
    data = merge_mappings(public_data, private_data)
    firmware = require_mapping(data, "firmware")
    backend = require_mapping(data, "backend")
    update_firmware_ota_url(firmware.get("ota_url"))
    write_backend_config(backend)
    print("Generated firmware/sdkconfig.defaults.private and ignored backend/config.yaml")


if __name__ == "__main__":
    main()
