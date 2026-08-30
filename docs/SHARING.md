# Safe sharing checklist

This repository deliberately tracks firmware and backend source while excluding
private deployment information and generated artifacts.

Before committing or pushing:

```bash
git status --short
git check-ignore private/local.yaml backend/config.yaml firmware/sdkconfig.defaults.private
rg -n -i "api[_-]?key|password|secret|private[_-]?key|credential" \
  --glob '!private/local.yaml' --glob '!backend/config.yaml' .
```

Review each search result: source code may legitimately refer to configuration
keys, but no real credential, server login, device token, or private endpoint
should be included.

Do not add build directories, firmware binaries, device dumps, recordings,
private tutorials, or editor/virtual-environment files. Use releases or a
separate artifact store for compiled firmware.
