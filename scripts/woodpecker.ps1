<#
.SYNOPSIS
Install, update, switch between and roll back releases of miainwoodpecker
on a Windows control computer.

.DESCRIPTION
Every release is installed side by side in a directory of its own and is
never changed after it has been verified. Which one starts is a pointer,
so switching - to a canary, or back from one - is rewriting a small file,
not reinstalling anything, and it needs no network.

    woodpecker install [stable|canary|<version>]   fetch, install and verify; do not switch
    woodpecker update  [stable|canary|<version>]   install if needed, then switch to it
    woodpecker use     <version>                   switch to an installed release
    woodpecker rollback                            switch back to the previous release
    woodpecker list                                what is installed, and what is current
    woodpecker start                               start the current release in the tray
    woodpecker autostart on|off                    start it at logon, or stop doing so
    woodpecker remove  <version>                   delete one release that is not in use
    woodpecker self-update                         take this script from the current release

A switch never touches a running session. It takes effect the next time
Woodpecker starts, and stopping the running one is left to the operator,
from its tray icon: stopping a scan is a decision about the sample, and
an updater is in no position to make it.

First installation, from a PowerShell prompt, needs no administrator:

    irm https://github.com/msarahan/miainwoodpecker/releases/latest/download/woodpecker.ps1 | iex

See docs/installing.md for the whole story, including how a release
becomes "stable" or "canary".

.NOTES
Written for Windows PowerShell 5.1, because that is what every control
computer has, and not for PowerShell 7, which few of them do: no `??`, no
ternaries, no three-argument Join-Path, and every file written without
the byte-order mark 5.1 adds by default - pixi's TOML reader refuses one.
#>
param(
    [Parameter(Position = 0)] [string] $Command,
    [Parameter(Position = 1)] [string] $Target,
    # A local source tree or release zip to install instead of a GitHub
    # release, named by $Target. For testing this script and a release
    # candidate before it is tagged; not how a microscope gets updated.
    [string] $From
)

function Invoke-Woodpecker {
    param([string] $Command, [string] $Target, [string] $From)

    # Inside a function, not at the top of the script: under `irm | iex`
    # the script runs in the caller's own scope, and these would otherwise
    # outlive it in the operator's shell.
    $ErrorActionPreference = 'Stop'
    $ProgressPreference = 'SilentlyContinue'  # 5.1's progress bar makes downloads ~10x slower
    Set-StrictMode -Version 2.0
    # 5.1 offers TLS 1.0 by default, which GitHub refuses.
    [Net.ServicePointManager]::SecurityProtocol = `
        [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12

    # Where releases come from. Overridable so a fork can deploy its own.
    $Repository = $env:WOODPECKER_REPOSITORY
    if (-not $Repository) { $Repository = 'msarahan/miainwoodpecker' }

    # pixi, pinned and checked, rather than whatever is on PATH. The
    # lockfile format belongs to a pixi version, so the pixi that installs
    # a release is part of what was tested; and a control computer's PATH
    # is nobody's to rely on. Bump both lines together, from the release's
    # own .sha256 asset.
    $PixiVersion = '0.77.0'
    $PixiSha256 = 'f7a879f9de570bc4de1b2a4748f00127bf173d72c2021e8d9b3ff3a182d63baa'

    # The three environments the tray runs across (see the `tray` task in
    # pyproject.toml): the vendor stack, the Qt window, and the dashboard.
    # All three are installed and checked before a release can be chosen,
    # so a switch never discovers a missing one halfway through a start.
    $Environments = @('default', 'device', 'dashboard')

    # Current, previous, and the newest one besides: enough to roll back
    # twice, and pixi hard-links packages out of its cache, so a kept
    # release costs little beyond its own changes.
    $KeepReleases = 3

    $Root = $env:WOODPECKER_HOME
    if (-not $Root) { $Root = Join-Path $env:LOCALAPPDATA 'miainwoodpecker' }
    $Releases = Join-Path $Root 'versions'
    $Logs = Join-Path $Root 'logs'
    $StatePath = Join-Path $Root 'state.json'
    $PixiExe = Join-Path (Join-Path $Root 'bin') 'pixi.exe'
    $Manager = Join-Path $Root 'woodpecker.ps1'
    $VerifiedMarker = '.woodpecker-verified'
    $UserConfig = Join-Path $HOME '.miainwoodpecker'

    function Say([string] $Message) { Write-Host "woodpecker: $Message" }

    function Write-Utf8([string] $Path, [string] $Text) {
        [IO.File]::WriteAllText($Path, $Text, (New-Object Text.UTF8Encoding $false))
    }

    function Read-State {
        $state = @{ current = $null; previous = $null }
        if (Test-Path $StatePath) {
            $saved = Get-Content -Raw $StatePath | ConvertFrom-Json
            $state.current = $saved.current
            $state.previous = $saved.previous
        }
        return $state
    }

    function Write-State($State) {
        # Written beside and then swapped in, so a power cut mid-write
        # leaves the old pointer rather than half of a new one - the one
        # file whose loss would leave nothing to start.
        $staged = "$StatePath.new"
        Write-Utf8 $staged ((@{ current = $State.current; previous = $State.previous } | ConvertTo-Json) + "`n")
        if (Test-Path $StatePath) {
            # [NullString], not $null: PowerShell hands $null to a string
            # parameter as "", which Replace rejects as a backup path.
            [IO.File]::Replace($staged, $StatePath, [NullString]::Value)
        } else {
            Move-Item $staged $StatePath
        }
    }

    function Get-ReleaseDir([string] $Version) { Join-Path $Releases $Version }

    function Test-Verified([string] $Version) {
        Test-Path (Join-Path (Get-ReleaseDir $Version) $VerifiedMarker)
    }

    function Get-Installed {
        if (-not (Test-Path $Releases)) { return @() }
        @(Get-ChildItem $Releases -Directory |
            Where-Object { Test-Path (Join-Path $_.FullName $VerifiedMarker) } |
            ForEach-Object {
                $marker = Get-Content -Raw (Join-Path $_.FullName $VerifiedMarker) | ConvertFrom-Json
                [pscustomobject]@{ Version = $_.Name; Installed = [datetime] $marker.installed; Source = $marker.source }
            } | Sort-Object Installed -Descending)
    }

    function Get-RunningRelease {
        # Which release has processes up, found by where their executables
        # live: every Python a session starts - broker, device servers,
        # windows - runs out of its release's own environments.
        $prefix = $Releases.TrimEnd('\') + '\'
        foreach ($process in Get-Process) {
            $path = $null
            try { $path = $process.Path } catch { }
            if ($path -and $path.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase)) {
                return $path.Substring($prefix.Length).Split('\')[0]
            }
        }
        return $null
    }

    function Install-Pixi {
        if (Test-Path $PixiExe) {
            if ((Get-FileHash $PixiExe -Algorithm SHA256).Hash -eq $PixiSha256) { return }
            Say "replacing $PixiExe, which is not pixi $PixiVersion"
        }
        New-Item -ItemType Directory -Force (Split-Path $PixiExe) | Out-Null
        $url = "https://github.com/prefix-dev/pixi/releases/download/v$PixiVersion/pixi-x86_64-pc-windows-msvc.exe"
        Say "downloading pixi $PixiVersion"
        $staged = "$PixiExe.download"
        Invoke-WebRequest -UseBasicParsing $url -OutFile $staged
        $hash = (Get-FileHash $staged -Algorithm SHA256).Hash
        if ($hash -ne $PixiSha256) {
            Remove-Item $staged
            throw "pixi download from $url has SHA-256 $hash, not the pinned $PixiSha256; refusing to run it"
        }
        Move-Item -Force $staged $PixiExe
    }

    function Invoke-Pixi {
        # To the console, not the pipeline: a function's uncaptured output
        # is its return value in PowerShell, and pixi's would otherwise
        # arrive in front of the version Install-Release returns.
        & $PixiExe @args | Out-Host
        if ($LASTEXITCODE -ne 0) { throw "pixi $($args -join ' ') failed with exit status $LASTEXITCODE" }
    }

    function Use-ReleaseEnvironment([string] $Version) {
        # A release is installed from a GitHub source archive, which has no
        # .git in it, and the version hatch-vcs would read from git has to
        # come from somewhere: this is setuptools-scm's documented way to
        # say it. Set for every pixi call, not only the first install -
        # pixi rebuilds an editable package when it decides it is stale,
        # and a rebuild with neither git nor this fails.
        # The plain form, not ..._FOR_MIAINWOODPECKER: hatch-vcs's backend
        # (vcs-versioning) is not told the distribution name, so the
        # per-package form never matches - measured, it says so in the
        # build error. The plain form would also reach any other package
        # built from source here, and nothing is: every other dependency
        # in the lock is a conda package or a wheel.
        $env:SETUPTOOLS_SCM_PRETEND_VERSION = $Version
        # And no pixi call on a release - including the ones the launcher
        # makes to reach the other environments - may re-solve. A release
        # is what was tested; a solve is today's newest versions.
        $env:PIXI_FROZEN = 'true'
    }

    function Get-GitHub([string] $Url, [string] $Looking) {
        try {
            Invoke-RestMethod -UseBasicParsing $Url
        } catch {
            # A bare "(404) Not Found" tells an operator nothing; what was
            # being looked for, and where, tells them whether it is a typo,
            # a release not published yet, or the network.
            throw "could not find $Looking on GitHub ($Url): $($_.Exception.Message)"
        }
    }

    function Resolve-Release([string] $Wanted) {
        if (-not $Wanted) { $Wanted = 'stable' }
        $api = "https://api.github.com/repos/$Repository/releases"
        if ($Wanted -eq 'stable') {
            # /latest skips drafts and pre-releases, which is exactly
            # the definition of stable used here.
            $release = Get-GitHub "$api/latest" "a stable release of $Repository"
        } elseif ($Wanted -eq 'canary') {
            # The newest published release of any kind. When the newest is
            # a stable one, canary and stable are the same, as they should
            # be: there is nothing newer to try.
            # Through a variable, not a pipe: 5.1's Invoke-RestMethod emits
            # a JSON array as one object, and piped straight into
            # Where-Object that one object is filtered as a whole - measured,
            # zero releases survive. A variable holding it enumerates.
            $all = Get-GitHub "$api`?per_page=30" "the releases of $Repository"
            $release = @($all | Where-Object { -not $_.draft })
            if (-not $release) { throw "$Repository has no published releases" }
            $release = $release[0]
        } else {
            $tag = $Wanted
            if ($tag -notmatch '^v') { $tag = "v$tag" }
            $release = Get-GitHub "$api/tags/$tag" "a release of $Repository tagged $tag"
        }
        [pscustomobject]@{ Tag = $release.tag_name; Version = $release.tag_name -replace '^v', '' }
    }

    function Remove-Release([string] $Version) {
        $dir = Get-ReleaseDir $Version
        if (-not (Test-Path $dir)) { return }
        # rmdir on the \\?\ form of the path rather than Remove-Item: an
        # environment is tens of thousands of files, and a half-installed
        # one can hold paths past MAX_PATH that only that form reaches.
        # The redirect is cmd's, inside the quotes: 5.1 turns a native
        # command's stderr into error records, which 'Stop' then throws.
        cmd /c "rmdir /s /q `"\\?\$dir`" >nul 2>nul"
        if (Test-Path $dir) { throw "could not remove $dir entirely; is something still using it?" }
    }

    function Assert-PathsFit([string] $Dir) {
        # Windows refuses paths past 260 characters unless long paths are
        # switched on for the whole machine, and an environment nests deep:
        # the deepest file in `default` sits 143 characters below its
        # prefix, measured, and the prefix is the release directory plus
        # "\.pixi\envs\dashboard". 150 leaves a little for a package that
        # grows. Refused here, before anything is downloaded, because the
        # failure otherwise is pixi "failing to persist" some file halfway
        # through an install - true, and no help at all.
        $enabled = $false
        try {
            $enabled = (Get-ItemProperty 'HKLM:\SYSTEM\CurrentControlSet\Control\FileSystem').LongPathsEnabled -eq 1
        } catch { }
        $needed = $Dir.Length + '\.pixi\envs\dashboard'.Length + 150
        if (-not $enabled -and $needed -gt 259) {
            throw ("$Dir is too deep for this release's environments: they need paths about $needed characters " +
                "long, and Windows allows 259 unless long paths are enabled. Set WOODPECKER_HOME to a " +
                "shorter directory, or ask IT to enable long paths (LongPathsEnabled).")
        }
    }

    function Copy-Source([string] $Source, [string] $Destination) {
        if ((Test-Path $Source -PathType Leaf) -and $Source -like '*.zip') {
            $unpacked = "$Destination.unpack"
            Expand-Archive $Source $unpacked
            $inner = @(Get-ChildItem $unpacked)
            # A GitHub archive holds one directory named after the
            # repository and tag; a hand-made zip may hold the tree itself.
            if ($inner.Count -eq 1 -and $inner[0].PSIsContainer) { $top = $inner[0].FullName } else { $top = $unpacked }
            Move-Item $top $Destination
            if (Test-Path $unpacked) { Remove-Item -Recurse -Force $unpacked }
        } elseif (Test-Path $Source -PathType Container) {
            # A working tree, for testing: everything a release archive
            # would hold and nothing it would not - no environments, no
            # caches, and no stale _version.py for the build to trust.
            robocopy $Source $Destination /E /NFL /NDL /NJH /NJS /NP `
                /XD .git .pixi .claude __pycache__ .pytest_cache .ruff_cache _build `
                /XF .git _version.py | Out-Null
            if ($LASTEXITCODE -ge 8) { throw "copying $Source failed (robocopy status $LASTEXITCODE)" }
        } else {
            throw "$Source is neither a directory nor a .zip"
        }
    }

    function Install-Release([string] $Wanted) {
        if ($From) {
            if (-not $Wanted -or $Wanted -in @('stable', 'canary')) {
                throw "-From needs the version to install it as, e.g. 'install 0.5.0rc1 -From .'"
            }
            $version = $Wanted -replace '^v', ''
            $source = (Resolve-Path $From).Path
        } else {
            $release = Resolve-Release $Wanted
            $version = $release.Version
            $source = "https://github.com/$Repository/archive/refs/tags/$($release.Tag).zip"
        }
        $dir = Get-ReleaseDir $version
        if (Test-Verified $version) {
            Say "$version is already installed"
            return $version
        }
        Assert-PathsFit $dir
        Install-Pixi
        New-Item -ItemType Directory -Force $Releases | Out-Null
        if (Test-Path $dir) {
            Say "removing an earlier, unfinished installation of $version"
            Remove-Release $version
        }

        Say "installing $version from $source"
        try {
            if ($From) {
                Copy-Source $source $dir
            } else {
                $zip = Join-Path $Releases ".download-$version.zip"
                Invoke-WebRequest -UseBasicParsing $source -OutFile $zip
                try { Copy-Source $zip $dir } finally { Remove-Item -Force $zip }
            }

            # Each release keeps its environments inside its own directory.
            # The checked-in .pixi/config.toml says the opposite, for a
            # reason that belongs to a checkout in a OneDrive folder and
            # not to this one; here, keeping them inside is what makes a
            # release one directory that is deleted whole.
            New-Item -ItemType Directory -Force (Join-Path $dir '.pixi') | Out-Null
            Write-Utf8 (Join-Path (Join-Path $dir '.pixi') 'config.toml') "detached-environments = false`n"

            Use-ReleaseEnvironment $version
            $manifest = Join-Path $dir 'pyproject.toml'
            foreach ($environment in $Environments) {
                Say "installing the $environment environment"
                Invoke-Pixi install --frozen --manifest-path $manifest -e $environment
            }

            # Verified here, on this computer, before it can be chosen: each
            # environment imports the package and agrees on what it is, and
            # the unit suite passes. A release that cannot do that on this
            # machine is removed now, rather than found out at the next
            # start with the instrument waiting.
            $reported = @{}
            foreach ($environment in $Environments) {
                $said = & $PixiExe run --frozen --manifest-path $manifest -e $environment `
                    python -c "import miainwoodpecker; print(miainwoodpecker.__version__)"
                if ($LASTEXITCODE -ne 0) { throw "the $environment environment cannot import miainwoodpecker" }
                $reported[$environment] = "$said".Trim()
            }
            $distinct = @($reported.Values | Sort-Object -Unique)
            if ($distinct.Count -ne 1) {
                throw "the environments disagree about which release they hold: $(($reported.GetEnumerator() | ForEach-Object { "$($_.Key)=$($_.Value)" }) -join ', ')"
            }
            Say "running the unit suite"
            Invoke-Pixi run --frozen --manifest-path $manifest -e default test -q

            Write-Utf8 (Join-Path $dir $VerifiedMarker) ((@{
                version = $distinct[0]
                source = $source
                installed = (Get-Date).ToString('o')
            } | ConvertTo-Json) + "`n")
        } catch {
            Say "installing $version failed; removing it"
            try { Remove-Release $version } catch { Say $_.Exception.Message }
            throw
        }

        # The first release installed becomes current: there is nothing
        # else to start, and "installed but not in use" would be one more
        # step for somebody setting up a computer.
        $state = Read-State
        if (-not $state.current) {
            $state.current = $version
            Write-State $state
        }
        if (-not (Test-Path $Manager)) { Install-Manager $version }
        Say "$version is installed and verified"
        Remove-OldReleases
        return $version
    }

    function Install-Manager([string] $Version) {
        # From a release, not from wherever this copy came from: under
        # `irm | iex` there is no file to copy, and the release is the copy
        # that was reviewed. Only ever replaced on request (self-update),
        # because this script is what a rollback runs - it must not change
        # underneath one.
        New-Item -ItemType Directory -Force $Root | Out-Null
        Copy-Item -Force (Join-Path (Join-Path (Get-ReleaseDir $Version) 'scripts') 'woodpecker.ps1') $Manager
        Write-Utf8 (Join-Path $Root 'woodpecker.cmd') `
            "@powershell.exe -NoProfile -ExecutionPolicy Bypass -File `"%~dp0woodpecker.ps1`" %*`r`n"
        if ($env:WOODPECKER_HOME) {
            # An installation somewhere other than the default is somebody
            # testing this script, and the account's PATH and Start menu
            # are not an experiment's to change.
            return
        }
        $userPath = [Environment]::GetEnvironmentVariable('Path', 'User')
        if (-not $userPath) { $userPath = '' }
        if (-not (($userPath -split ';') -contains $Root)) {
            [Environment]::SetEnvironmentVariable('Path', ($userPath.TrimEnd(';') + ";$Root").TrimStart(';'), 'User')
            Say "added $Root to your PATH; open a new terminal to use 'woodpecker'"
        }
        New-Shortcut (Join-Path ([Environment]::GetFolderPath('Programs')) 'Woodpecker.lnk')
    }

    function New-Shortcut([string] $Path) {
        $shell = New-Object -ComObject WScript.Shell
        $link = $shell.CreateShortcut($Path)
        $link.TargetPath = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
        $link.Arguments = "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$Manager`" start"
        $link.WorkingDirectory = $HOME
        $link.Description = 'Hold the microscope open from the notification area'
        $link.Save()
    }

    function Switch-Release([string] $Version) {
        if (-not (Test-Verified $Version)) {
            throw "$Version is not installed; 'woodpecker install $Version' first, or 'woodpecker list' for what is"
        }
        $state = Read-State
        if ($state.current -eq $Version) {
            Say "$Version is already current"
            return
        }
        $state.previous = $state.current
        $state.current = $Version
        Write-State $state
        Say "$Version is now current (previous: $($state.previous))"
        $running = Get-RunningRelease
        if ($running -and $running -ne $Version) {
            Say "$running is running now and keeps running. The switch takes effect the next time Woodpecker starts:"
            Say "stop it from its tray icon when the instrument can be stopped, then start Woodpecker again."
        }
    }

    function Remove-OldReleases {
        $state = Read-State
        $running = Get-RunningRelease
        $kept = 0
        foreach ($release in Get-Installed) {
            $protected = $release.Version -in @($state.current, $state.previous, $running)
            if ($protected -or $kept -lt $KeepReleases) {
                $kept += 1
                continue
            }
            Say "removing $($release.Version), which is older than the $KeepReleases kept"
            Remove-Release $release.Version
        }
    }

    function Start-Current {
        $state = Read-State
        if (-not $state.current) { throw "nothing is installed yet; 'woodpecker install' first" }
        $running = Get-RunningRelease
        if ($running) {
            # One session per computer: two brokers would each try to open
            # the same hardware. The tray already running is the answer to
            # "start", not something to start beside.
            throw "Woodpecker $running is already running; it is in the notification area"
        }
        $dir = Get-ReleaseDir $state.current
        Use-ReleaseEnvironment $state.current
        $manifest = Join-Path $dir 'pyproject.toml'
        # The `tray` task in pyproject.toml, spelled out rather than run by
        # name: it ends in `-- marimo run ...`, so anything appended to it
        # would land in marimo's command line rather than the tray's.
        $arguments = @(
            'run', '--frozen', '--manifest-path', "`"$manifest`"", '-e', 'default',
            'miainwoodpecker-tray', '--broker-env', 'device', '--ui-env', 'default', '--dashboard-env', 'dashboard'
        )
        $instrument = Join-Path $UserConfig 'instrument.toml'
        if (Test-Path $instrument) { $arguments += @('--config', "`"$instrument`"") }
        $notebook = Join-Path (Join-Path $dir 'notebooks') 'instrument_dashboard.py'
        $arguments += @('--', 'marimo', 'run', "`"$notebook`"")

        # No console to read, so what a session says goes to a file per
        # start, named for the release: the first thing to look at when a
        # canary misbehaves is what it printed.
        New-Item -ItemType Directory -Force $Logs | Out-Null
        $stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
        $log = Join-Path $Logs "$($state.current)-$stamp"
        Get-ChildItem $Logs -Filter '*.log' | Sort-Object LastWriteTime -Descending |
            Select-Object -Skip 40 | Remove-Item -Force
        Start-Process -FilePath $PixiExe -ArgumentList $arguments -WorkingDirectory $HOME `
            -WindowStyle Hidden -RedirectStandardOutput "$log.out.log" -RedirectStandardError "$log.err.log"
        Say "started $($state.current); logging to $log.*.log"
    }

    function Show-List {
        $state = Read-State
        $running = Get-RunningRelease
        $installed = Get-Installed
        if (-not $installed) { Say 'nothing is installed'; return }
        foreach ($release in $installed) {
            $notes = @()
            if ($release.Version -eq $state.current) { $notes += 'current' }
            if ($release.Version -eq $state.previous) { $notes += 'previous' }
            if ($release.Version -eq $running) { $notes += 'running' }
            $mark = ' '
            if ($release.Version -eq $state.current) { $mark = '*' }
            $label = ''
            if ($notes) { $label = "($($notes -join ', '))" }
            Write-Host ("{0} {1,-20} {2,-24} installed {3:yyyy-MM-dd}" -f $mark, $release.Version, $label, $release.Installed)
        }
        try {
            $stable = (Resolve-Release 'stable').Version
            $canary = (Resolve-Release 'canary').Version
            Write-Host ''
            Write-Host "available: stable $stable, canary $canary"
        } catch {
            # Listing what is here must work offline; what is out there
            # is a nicety.
            Write-Host ''
            Write-Host "(could not ask GitHub what is available: $($_.Exception.Message))"
        }
    }

    function Set-Autostart([string] $Setting) {
        $startup = Join-Path ([Environment]::GetFolderPath('Startup')) 'Woodpecker.lnk'
        if ($Setting -eq 'on') {
            New-Shortcut $startup
            Say 'Woodpecker will start when you log on'
        } elseif ($Setting -eq 'off') {
            if (Test-Path $startup) { Remove-Item $startup }
            Say 'Woodpecker will no longer start when you log on'
        } else {
            throw "autostart takes 'on' or 'off'"
        }
    }

    switch ($Command) {
        'install' { Install-Release $Target | Out-Null }
        'update' { Switch-Release (Install-Release $Target) }
        'use' {
            if (-not $Target) { throw "use which release? 'woodpecker list' shows what is installed" }
            Switch-Release ($Target -replace '^v', '')
        }
        'rollback' {
            $state = Read-State
            if (-not $state.previous) { throw 'there is no previous release to roll back to' }
            Switch-Release $state.previous
        }
        'list' { Show-List }
        'start' { Start-Current }
        'autostart' { Set-Autostart $Target }
        'remove' {
            $version = $Target -replace '^v', ''
            $state = Read-State
            if ($version -eq $state.current) { throw "$version is current; switch to another release first" }
            if ($version -eq (Get-RunningRelease)) { throw "$version is running; stop it first" }
            Remove-Release $version
            if ($version -eq $state.previous) { $state.previous = $null; Write-State $state }
            Say "removed $version"
        }
        'self-update' {
            $state = Read-State
            if (-not $state.current) { throw 'nothing is installed to update from' }
            Install-Manager $state.current
            Say "this script is now the one from $($state.current)"
        }
        default { Get-Help $PSCommandPath -Detailed | Out-Host }
    }
}

# `irm ... | iex` runs this with no file and no arguments, which is the
# first installation; with a file and no arguments, it is a request for
# help.
if (-not $Command -and -not $PSCommandPath) { $Command = 'install' }
try {
    Invoke-Woodpecker -Command $Command -Target $Target -From $From
} catch {
    Write-Host "woodpecker: $($_.Exception.Message)" -ForegroundColor Red
    # `exit` under iex would close the operator's own shell.
    if ($PSCommandPath) { exit 1 }
}
