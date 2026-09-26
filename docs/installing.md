# Installing on a control computer

This page covers putting miainwoodpecker on the Windows computer that
drives a microscope, keeping it up to date, trying a new release before
everyone else does, and going back when one misbehaves. For a
development checkout, `pixi run preview` in a clone is still the way
in; see the [README](https://github.com/msarahan/miainwoodpecker#readme).

## Installing

From a PowerShell prompt, as the account that runs the microscope (no
administrator needed):

```powershell
irm https://github.com/msarahan/miainwoodpecker/releases/latest/download/woodpecker.ps1 | iex
```

That downloads a pinned, checksummed pixi, installs the latest stable
release, checks it on this computer (below), and adds **Woodpecker** to
the Start menu and `woodpecker` to your PATH. Start it from the Start
menu; it lives in the notification area from then on. To have it start
at logon:

```powershell
woodpecker autostart on
```

It serves the instrument described by
`%USERPROFILE%\.miainwoodpecker\instrument.toml` if there is one, and the
simulated instrument if there is not. Hardware is never what you get by
leaving something out. See [instrument configuration](instrument-configuration.md)
for writing that file. It lives outside every release, so installing,
switching and rolling back leave it alone.

Nothing else is needed on the computer: no Python, no git, no compiler.
It does need to reach GitHub and conda-forge when installing a release,
and does not need either when switching between installed ones.

## Updating, canaries and rolling back

Every release is installed side by side in a directory of its own and
never changed after it has been checked. Which release starts is a
pointer. So switching, in either direction, rewrites one small file:

```powershell
woodpecker update           # install the latest stable release and switch to it
woodpecker update canary    # the same, for the newest release of any kind
woodpecker rollback         # back to the release that was current before
woodpecker use 0.4.0        # any installed release, by name
woodpecker list             # what is installed, current, previous, running, and available
```

**A switch never touches a running session.** It takes effect the next
time Woodpecker starts. Stopping the running one is left to the
operator, from its tray icon, because stopping a scan is a decision
about the sample and an updater is in no position to make it. `update`
says so when a session is running.

**A release is checked before it can be chosen.** `install` and `update`
install all three environments the tray runs across (the vendor device
stack, the Qt window, and the browser dashboard). They then confirm
that each environment imports the entry points a session runs from it
and that all three agree on the version. This is a smoke check of the
installation on this computer. The release itself was tested before it
was tagged. A release that fails any of that is removed on the
spot and the current one is untouched. Finding out at the next start,
with the instrument waiting, would be worse.

**Rolling back needs no network**, because the previous release is still
installed. The three most recent releases are kept, plus whichever are
current, previous and running. Older ones are removed after each install.

`install` does everything `update` does except switch, which is how to
stage a release in advance and switch later.

### What "stable" and "canary" mean

They are GitHub releases, read from the repository at the moment you
ask:

- **stable** is the latest release that is *not* marked as a
  pre-release.
- **canary** is the newest published release of any kind. When that is a
  stable one, canary and stable are the same release, since there is
  nothing newer to try.

There is no channel to subscribe a computer to. A microscope is on a
canary because someone ran `woodpecker update canary` on it, and it
leaves by `woodpecker rollback` or `woodpecker update`.

### A notebook left open across a switch

The broker and everything that joins it (the window, the dashboard, a
notebook) must be the same release. They exchange this project's own
objects, and nothing promises those keep their shape between releases.
The broker writes its release into the `broker.json` it publishes, and a
client of a different release is refused at the door with both versions
named, rather than failing halfway through a scan. After a switch,
restart notebooks from the new release's environment.

That check covers the broker invitation only. A device server attached
from inside a vendor's own Python (the Gatan bridge in GMS) is deployed
separately and keeps its own protocol version.

## Where things are

```
%LOCALAPPDATA%\miainwoodpecker\
  woodpecker.ps1, woodpecker.cmd   this script, and the shim that puts it on PATH
  bin\pixi.exe                     pinned pixi, checked against a SHA-256
  versions\<release>\              one release: its source and its environments
  state.json                       {"current": ..., "previous": ...}
  logs\<release>-<time>.*.log      what each session printed; the first place to look
%USERPROFILE%\.miainwoodpecker\    instrument.toml and broker.json, shared by every release
```

`WOODPECKER_HOME` puts the whole installation somewhere else. An
installation there leaves the PATH and the Start menu alone, which is
what makes it safe to try things with.

**Long paths.** A release's environments nest deep: the deepest file sits
143 characters below its environment directory. Windows refuses paths
past 259 characters unless long paths are enabled for the machine, so
`woodpecker` refuses an installation directory deep enough to hit that
before downloading anything. The default location is well inside the
limit. If you move it somewhere deep, ask IT to set `LongPathsEnabled`.

## The script itself

`woodpecker.ps1` is what a rollback runs, so it does not change
underneath one. It is copied from the first release installed and
replaced only when you ask:

```powershell
woodpecker self-update      # take the copy from the current release
```

## Making a release

1. Tag the commit `vX.Y.Z`, or `vX.Y.ZrcN` for a candidate, and push
   the tag.
2. Publish the release **with the script attached, in one command**:

   ```bash
   git show vX.Y.Z:scripts/woodpecker.ps1 > woodpecker.ps1
   gh release create vX.Y.Z woodpecker.ps1 --verify-tag --notes-file notes.md
   ```

   Add `--prerelease` for a candidate. That flag is the whole difference
   between a canary and a stable release.

   Don't publish from the web page and attach the script afterwards.
   This repository has **immutable releases** on, so a published release
   takes no new assets, and the one-line installer downloads
   `releases/latest/download/woodpecker.ps1`. v0.1.0 went out that way,
   and the installer returned 404 until v0.1.1. Given a file,
   `gh release create` makes a draft, uploads the file, and only then
   publishes, which immutable releases allow. The script comes from
   `git show` rather than the working tree so it is byte for byte the
   tagged one.
3. The release workflow checks that the attached script matches the
   tagged one, and fails loudly if it is missing or different. It also
   publishes to PyPI once the repository variable `PUBLISH_TO_PYPI` is
   `true`, which waits on a trusted publisher being registered on
   pypi.org (see the `publish` job). The installer doesn't use PyPI
   either way.
4. Try it on one microscope with `woodpecker update canary` before
   promoting it. Promoting means publishing the stable tag. Every other
   microscope then picks it up with `woodpecker update`.

To try a candidate on a computer before tagging anything, install a
working tree or a zip as a named release:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\woodpecker.ps1 install 0.5.0rc1 -From .
```

Two rules keep rollback honest:

- **A configuration file must stay readable by the previous release.**
  `instrument.toml` refuses unknown keys, which is right, but it means a
  new key in schema 1 stops an older release starting the moment someone
  uses it. A change an older release cannot read needs a new `schema`
  number, and the older release's refusal then names the reason.
- **Bump pixi deliberately.** The script pins pixi's version and
  checksum (`$PixiVersion`, `$PixiSha256`), because the lockfile format
  belongs to a pixi version. Re-locking with a newer pixi means bumping
  both in the same change.
