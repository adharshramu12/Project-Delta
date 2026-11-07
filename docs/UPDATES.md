# Updates

Delta tells you when a newer version is out and updates itself when you press **Update now**. It never installs
anything by itself.

## What you see

- A banner at the top of the dashboard: "Delta 0.4.0 is out", the first few highlights from the release notes, and
  **Update now**, **What's new**, **Later** (hides it for a day) and **Skip this version** (hides it until an even
  newer version comes out).
- With Telegram connected, one message per new version. It only tells you; updating is done on the PC.
- After an update, a "What's new" panel once.
- `delta doctor` shows the version on this PC and the newest one.

Settings > **Updates and help**: turn the checks off, include test versions (beta), turn the Telegram message off,
**Check now**, and **Take the tour**.

## How Delta checks

Soon after Delta starts (20 to 90 seconds later), then every 12 hours with a little jitter, Delta asks GitHub:

1. **The latest GitHub Release** of `adharshramu12/Project-Delta`
   (`https://api.github.com/repos/adharshramu12/Project-Delta/releases/latest`). Drafts are never offered.
   Pre-releases are offered only on the beta channel (then Delta reads the last 10 releases and takes the highest
   version). Versions are compared as [Semantic Versioning](https://semver.org) says: `0.10.0` is newer than `0.9.0`,
   and `0.4.0-beta.1` is older than `0.4.0`.
2. **No release yet:** the `version` in `pyproject.toml` on the main branch
   (`https://raw.githubusercontent.com/adharshramu12/Project-Delta/main/pyproject.toml`), with the matching
   `## <version>` section of `CHANGELOG.md` as the highlights.

What is sent: plain HTTPS GET requests with the header `User-Agent: Delta/<version>`. No id, no usage data, nothing
about your PC. A proxy set in Windows or in `HTTPS_PROXY` is used.

Being a good citizen with GitHub's limits (60 requests an hour per address without a sign-in):

- Answers are cached with their ETag. An unchanged answer comes back as "304 Not Modified", which GitHub does not
  count against the limit.
- A rate limit stops all checks until GitHub's reset time (at least 10 minutes, at most a day).
- An offline PC or a GitHub outage is retried later, backing off from 30 minutes up to the 12-hour interval. Nothing
  is shown to you for it except a line in Settings > Updates and help, and nothing goes to the crash log.
- **Check now** asks again at most once a minute.

What Delta remembers is in `%LOCALAPPDATA%\Delta\updates.json`: the newest version found, when it last checked,
the cached answers, Later and Skip this version, and which versions were already sent to Telegram.

Release notes are cleaned to plain text on the PC (no HTML, no pictures, link text without the address, no hidden
or direction-changing characters) and the dashboard sets them as text, never as HTML. Nothing in release notes is
ever run.

## What Update now does

1. **Asks first**, and waits for a running task. If one is running, it asks whether to stop it.
2. **Pins the exact commit.** It asks GitHub which commit the release tag (for example `v0.4.0`) points to, then
   reads that commit's `pyproject.toml` and `uv.lock` from `raw.githubusercontent.com` and keeps their SHA-256. The
   version in that `pyproject.toml` must be the release's version, and `uv.lock` must be Delta's lock file, or the
   update stops before anything is downloaded for real. (With no release yet, it pins main's current commit, which
   must still be newer than yours.)
3. **Starts `delta update`** with that commit, the repository and the two hashes. It runs Delta's own installer
   (`scripts/install.ps1`) in a window that shows each step.
4. **The installer checks before it changes anything.** It downloads `https://github.com/<repo>/archive/<commit>.zip`
   (that exact commit, over HTTPS), and checks that `pyproject.toml` and `uv.lock` in it are byte for byte the files
   from step 2 and that the version matches. If anything differs, it stops and Delta keeps running as it was.
5. **Installs.** It closes Delta (as Shut down does), copies the new files, and installs the exact package versions
   in `uv.lock` (`uv sync --frozen`), each checked against its hash in the lock. Your data folder
   (`%LOCALAPPDATA%\Delta`: chats, memory, settings, logins) is never touched; keys stay in Windows Credential
   Manager.
6. **Opens Delta again**, which shows What's new.

`delta update` in a terminal does the same pinning and checks. If GitHub cannot be asked (offline, rate limit), it
says so and updates from the main branch over HTTPS as before. `DELTA_REF` (a branch, tag or commit) and
`DELTA_REPO` (a fork) pick another version, as they do for the installer.

## Turning it off

- Settings > Updates and help > untick **Check for new versions**, or in `%LOCALAPPDATA%\Delta\settings.yaml`:

  ```yaml
  updates:
    check: false        # never ask GitHub
    channel: stable     # or beta: test versions too
    telegram: true      # one message per new version
  ```

- `DELTA_NO_UPDATE_CHECK=1` in the environment does the same for that Delta (the test suite uses it).

## For the maintainer: publishing a release

Users see release notes only for **tagged GitHub Releases**. Until the first one exists, Delta falls back to the
version in `pyproject.toml` on main, with the matching CHANGELOG section as notes, so the banner already works.

To publish a version:

1. Bump `version` in `pyproject.toml` (and run `uv lock`, which updates Delta's own entry in `uv.lock`), add a
   `## <version>` section to `CHANGELOG.md`, and merge.
2. On GitHub, **Releases > Draft a new release**, create the tag `v<version>` (for example `v0.3.0`) on that merge
   commit, paste the CHANGELOG section as the notes, and publish. Leave it as a draft until you are ready: drafts
   are never offered. Tick **Set as a pre-release** for a test version: only people on the beta channel see it.
3. The tag's `pyproject.toml` must say the same version as the tag, or Update now refuses it.

Never move a published tag. Update now pins the commit the tag points to at the time someone presses it.

## For tests

`DELTA_UPDATE_API` and `DELTA_UPDATE_RAW` point the check at a stand-in GitHub on this PC
(`tests/fake_github.py`). Only an address on 127.0.0.1 or localhost is accepted.
