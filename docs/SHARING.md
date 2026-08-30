# Safe sharing checklist

This repository deliberately tracks firmware, backend source, and the approved
team-shared endpoints in `public.yaml`, while excluding private deployment
information and generated artifacts.

Before committing or pushing:

```bash
git status --short
git check-ignore private/local.yaml backend/config.yaml firmware/sdkconfig.defaults.private
rg -n -i "api[_-]?key|password|secret|private[_-]?key|credential" \
  --glob '!private/local.yaml' --glob '!backend/config.yaml' .
```

Review each search result: source code may legitimately refer to configuration
keys, and `public.yaml` may contain the approved team endpoint. No credential,
server login, device token, password, or other private endpoint should be
included.

Do not add build directories, firmware binaries, device dumps, recordings,
private tutorials, or editor/virtual-environment files. Use releases or a
separate artifact store for compiled firmware.
