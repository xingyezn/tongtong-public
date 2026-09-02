# Configuration and deployment workflow

This repository is safe to share only because **source code and runtime secrets
are kept separate**. Treat the following as three different layers:

| Layer | Location | May contain real endpoints or secrets? | Git status |
| --- | --- | --- | --- |
| Shared source | tracked files, including `public.yaml` | No | Commit and push |
| Local build settings | `private/local.yaml` and generated files | Yes | Ignored |
| Production runtime settings | server `backend/config.yaml` and environment file | Yes | Never commit |

## 1. Shared repository rules

- Keep `public.yaml` exactly on its `your-server.example` placeholders.
- Commit firmware and backend source, examples, scripts, and documentation.
- Never commit real OTA/WS endpoints, server credentials, dashboard passwords,
  device tokens, API keys, generated configuration, build output, recordings,
  or production firmware binaries.
- Before every push, follow [SHARING.md](SHARING.md).

## 2. Local development and firmware build

### First-time setup

```bash
copy private\\local.example.yaml private\\local.yaml
python -m pip install -r backend/requirements.txt
python scripts/configure_local.py
```

Fill `private/local.yaml` with the real deployment values. The generator merges
those local values over the public-safe placeholders and creates two ignored
runtime files:

| Generated file | Purpose |
| --- | --- |
| `firmware/sdkconfig.defaults.private` | Real OTA endpoint used only by a private firmware build |
| `backend/config.yaml` | Backend host, public WS endpoint, dashboard settings, device policy, and model settings |

Keep `DASHSCOPE_API_KEY` out of both files when possible. Set it through the
environment of the process that runs the backend.

### Build a production firmware

After changing `private/local.yaml`, regenerate configuration. If the OTA value
changed, remove the existing `firmware/sdkconfig` before configuring again so
ESP-IDF cannot reuse the old value.

```bash
python scripts/configure_local.py
cd firmware
idf.py -D SDKCONFIG_DEFAULTS="sdkconfig.defaults;sdkconfig.defaults.private" set-target esp32s3
idf.py -D SDKCONFIG_DEFAULTS="sdkconfig.defaults;sdkconfig.defaults.private" build
```

推荐直接使用仓库脚本：

```powershell
powershell -ExecutionPolicy Bypass -File scripts/build_firmware.ps1
# 刷写：
powershell -ExecutionPolicy Bypass -File scripts/build_firmware.ps1 -Flash -Port COM3
```

固件 CMake 会拒绝 `your-server.example` 占位 OTA 地址；如果没有私有配置，构建会直接失败，不会生成可刷写的占位地址固件。

The second defaults file is loaded last, so its real OTA endpoint overrides the
tracked placeholder. A production binary therefore contains private deployment
data. Burn it to controlled devices or distribute it privately; do not commit
it or attach it to a public release.

To create a genuinely public demo firmware, build from a clean working tree
without `sdkconfig.defaults.private`, using only the tracked placeholder. It
will not connect to the private production service until configured separately.

## 3. Backend deployment to the server

The server needs real configuration to run. Deploy **redacted code plus private
runtime configuration**, not a redacted configuration file.

1. Push or upload only the tracked backend code.
2. On the local machine, run `python scripts/configure_local.py`.
3. Securely copy the ignored `backend/config.yaml` to the server's backend
   directory. Do not copy `private/local.yaml`; it is a workstation source file.
4. Create the server environment file with the API key, restrict its permission,
   then restart the systemd service.

Example commands, with your own server and deployment path:

```bash
# local machine: code may be delivered by git pull or an upload workflow
scp backend/config.yaml <server>:<app-dir>/config.yaml

# server
chmod 600 <app-dir>/config.yaml
printf 'DASHSCOPE_API_KEY=sk-your-key\n' > /etc/tongtong-omni.env
chmod 600 /etc/tongtong-omni.env
systemctl restart tongtong-omni
```

The supplied systemd unit loads `/etc/tongtong-omni.env`. Keep that file and
the server's `config.yaml` outside Git backups, public issue attachments, and
terminal transcripts.

## 4. Change checklist

### Code-only change

1. Change tracked source.
2. Run the applicable tests.
3. Run the sharing scan.
4. Commit/push source, then deploy code and restart the backend if needed.

### Deployment setting change

1. Change only `private/local.yaml`.
2. Run `python scripts/configure_local.py`.
3. For a firmware OTA change: rebuild with both defaults files and privately
   distribute the new firmware.
4. For a backend change: securely copy the regenerated `backend/config.yaml`
   to the server and restart the service.
5. Never edit a tracked placeholder to make a deployment work.

运行中的后端也可通过管理页面修改“对话连续时长（分钟）”（1～120，默认
10）。该值写入后端运行配置，后续新一轮对话会按新值判断空闲超时；设备重连
仍会立即创建全新的模型会话。

### Secret rotation

1. Update the API key in the server environment file, not in Git or the
   generated YAML.
2. Restart the service.
3. Revoke the old key at the provider.
4. If a secret was ever committed, rotate it immediately; deleting the file in
   a later commit does not remove it from Git history.

## 5. Multi-developer workflow

`main` is the stable integration branch. Do not develop directly on it; make
one focused feature or fix branch for each change.

```text
main
├── feat/mcp-tool-call
├── fix/audio-streaming
└── docs/config-workflow
```

### Start a change

```bash
git switch main
git pull --ff-only origin main
git switch -c feat/short-description
```

Use a prefix that explains the change: `feat/`, `fix/`, `docs/`, or `chore/`.
Keep unrelated formatting, configuration, and feature work in separate
branches where practical.

### Finish and integrate a change

```bash
# Run the relevant tests before committing.
git add <changed-files>
git commit -m "feat: short description"

# Incorporate work merged by other developers while this branch was active.
git fetch origin
git rebase origin/main

# Resolve any conflict on this branch, rerun tests, then publish it.
git push -u origin feat/short-description
```

Open a Pull Request and merge it after the author has verified the relevant
tests and the sharing scan. For a firmware change that controls real hardware,
first keep the feature branch local and complete a supervised hardware test;
only then push it and open the PR. **At the current project stage, peer approval is
not required:** the branch author may merge their own PR when it is ready.
Squash merge is preferred to keep `main` history concise. Delete the merged
feature branch, then update local `main` before starting the next task.

```bash
git switch main
git pull --ff-only origin main
```

### Conflict and safety rules

- Resolve conflicts on the feature branch before merging, not by editing
  `main` directly.
- After a rebase, update only your own feature branch with
  `git push --force-with-lease`; never use an unrestricted `--force` on a
  shared branch.
- Do not force-push, reset, or rewrite `main`.
- Each developer keeps their own ignored `private/local.yaml` and generated
  files. Never use Git to share private runtime configuration.
- Before merging firmware changes, compile the target board when practical.
  For changes that affect motors, power, relays, or other physical actuators,
  also perform a supervised hardware test (start with the mechanism unloaded
  or wheels off the ground) before the first remote push. Before merging backend
  changes, run the affected backend tests. Always run the sharing scan before a
  public push.
