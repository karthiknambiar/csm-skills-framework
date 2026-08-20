# Troubleshooting

Keep errors concise and sanitized. Never expose raw subprocess output, credentials, environment assignments, or private paths unrelated to the requested artifact.

| Situation | Exact command | Next action |
|---|---|---|
| Readiness unknown | `csaf setup doctor` | Report `ready`, `degraded`, or `failed` and one next action. |
| Missing or damaged component | `csaf setup repair` | Show the exact repair plan and obtain explicit consent before changes. |
| Stable update proposed | `csaf setup update` | Notify only until the user gives fresh explicit consent. |
| Update check requested | `csaf setup check-update` | Network failure is non-fatal; keep the installed runtime usable. |
| QBR preflight | `csaf office doctor --json` | If not ready, run diagnosis or consent-driven repair before customer data. |

## Runtime absent

The installed `csaf` command cannot repair an absent runtime. Present the launcher's `bootstrap_required` disclosure and obtain explicit consent before downloading or executing either platform installer:

- Windows stable installer: `https://github.com/karthiknambiar/csm-skills-framework/releases/latest/download/install.ps1`; after verified download, run `powershell -NoProfile -ExecutionPolicy Bypass -File <downloaded-install.ps1>`.
- macOS/Linux stable installer: `https://github.com/karthiknambiar/csm-skills-framework/releases/latest/download/install.sh`; after verified download, run `sh <downloaded-install.sh>`.

Explain that bootstrap uses verified tagged stable assets over HTTPS, requires no API key or hosted AI, and installs OfficeCLI locally because it is mandatory for QBR rendering. Do not download or execute after a vague “fix it” or “do not ask”; ask for explicit consent to the exact disclosed plan first.

Do not run update automatically. Do not replace the user's source or template after an invalid-template failure; offer the vetted bundled QBR template. Do not claim repair or artifact success until the command returns exit code 0 and the expected files pass verification.