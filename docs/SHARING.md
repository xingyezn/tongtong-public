# Safe sharing checklist

This repository deliberately tracks firmware, backend source, and only redacted
endpoint placeholders in `public.yaml`, while excluding all deployment
information and generated artifacts.

For the complete distinction between shareable source, local firmware builds,
and private server runtime settings, read [CONFIGURATION_WORKFLOW.md](CONFIGURATION_WORKFLOW.md).

Before committing or pushing:

```bash
git status --short
git check-ignore private/local.yaml backend/config.yaml firmware/sdkconfig.defaults.private
rg -n -i "api[_-]?key|password|secret|private[_-]?key|credential" \
  --glob '!private/local.yaml' --glob '!backend/config.yaml' .
```

Review each search result: source code may legitimately refer to configuration
keys, but no real endpoint, credential, server login, device token, or password
should be included. `public.yaml` must retain its `your-server.example` values.

Do not add build directories, firmware binaries, device dumps, recordings,
private tutorials, or editor/virtual-environment files. Use releases or a
separate artifact store for compiled firmware.
