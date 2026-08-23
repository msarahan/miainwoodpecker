"""
A live instrument dashboard in the browser, as a marimo app.

Run it against a broker that is already serving an instrument::

    miainwoodpecker-broker --publish .        # in one terminal
    marimo run notebooks/instrument_dashboard.py   # in another

``marimo edit`` instead of ``marimo run`` opens the same file with the
code visible, which is what you want while changing it. See
docs/scripting-and-automation.md for the whole procedure, including
where the invitation comes from.

**This notebook is a client, not a driver.** Every tile is a *watch*:
:meth:`~miainwoodpecker.broker.interface.InstrumentBroker.snapshot`
returns each target's state and its latest frames together, costs no
device call, and cannot start or stop anything. The only thing here that
drives is Acquire, and it does so inside a lease - which is the only way
to acquire, and which the broker grants to one client at a time. Nothing
in this file holds a device handle.

**Nothing here takes a lease on the cell that runs it.** Taking a lease
means waiting out the pass already in flight, and a pass is ``height x
width x dwell`` - up to minutes on a large slow scan. A cell that
blocked for that would freeze the kernel, and with it every tile on
screen at exactly the moment somebody wants to see what the instrument
is doing. Acquire therefore starts a
:class:`~miainwoodpecker.dashboard.acquisition.AcquisitionJob`, which
takes the lease on a worker thread, and the display learns how it went
from the same poll that draws the tiles.

**The log is append-only because marimo forbids the alternative.** A
cell cannot write new cells - the dependency graph is the notebook, and
a program that rewrites its own graph while running has no defined
order. So each acquisition adds an entry to a panel instead, which turns
out to be the better shape anyway: a shift's acquisitions in the order
they happened, in one place.

**Where the judgement lives.** Which targets get a tile, what the chrome
says, how a frame becomes pixels a browser will draw, and what an
acquisition records are all in :mod:`miainwoodpecker.dashboard`, not in
these cells. A marimo cell cannot be unit-tested without marimo's
runtime, and marimo is an optional dependency the test environments do
not install; a decision made inside one is a decision nothing checks.

**Layout.** The app runs full width and the tiles are laid out in fixed
rows, in ``describe()`` order - which is static for the life of the
instrument, so a tile never moves when a camera stops. That also means
marimo's own grid layout (View -> Grid in the editor, saved beside this
file) keeps meaning what it meant when it was saved; this file does not
ship one, because a hand-written layout file that disagrees with the
notebook is worse than none.

Needs the ``marimo`` optional-dependency group, and a broker to watch.
"""

import marimo

__generated_with = "0.9.0"
app = marimo.App(width="full")


@app.cell
def _():
    import marimo as mo

    from miainwoodpecker.dashboard import (
        AcquisitionJob,
        SessionLog,
        camera_request,
        channel_labels,
        connect_dashboard,
        frame_sources,
        frame_tiles,
        highlights,
        is_image,
        png_data_uri,
        scan_request,
        tile_status,
    )
    from miainwoodpecker.devices.interface import CameraParameters, ScanParameters
    from miainwoodpecker.storage.session import Session

    return (
        AcquisitionJob,
        CameraParameters,
        ScanParameters,
        Session,
        SessionLog,
        camera_request,
        channel_labels,
        connect_dashboard,
        frame_sources,
        frame_tiles,
        highlights,
        is_image,
        mo,
        png_data_uri,
        scan_request,
        tile_status,
    )


@app.cell
def _(mo):
    mo.md(
        """
        # Instrument dashboard

        Live tiles for every frame source the broker serves, and one
        Acquire action that takes a lease. Watching costs the instrument
        nothing; acquiring takes it away from everyone else for as long
        as the lease is held, and says so in their status lines.
        """,
    )
    return


@app.cell
def _(mo):
    # A form rather than a bare text box: connecting opens a socket on
    # the broker, and a text box that updated per keystroke would open
    # one per character and leave the rest for the server to reap.
    connection_form = mo.ui.text(
        label=(
            "Broker invitation - blank searches $MIAINWOODPECKER_BROKER, "
            "then ./broker.json"
        ),
        full_width=True,
    ).form(submit_button_label="Connect", bordered=False)
    connection_form
    return (connection_form,)


@app.cell
def _(connect_dashboard, connection_form, mo):
    try:
        broker = connect_dashboard(connection_form.value or None)
        described = broker.describe()
        connection_error = None
    except (OSError, ValueError) as error:
        broker = None
        described = {}
        connection_error = error
    # Stopping here rather than raising: the message is an instruction,
    # and it must not be a traceback the operator has to read past. It
    # also stops every cell below, which is right - there are no tiles
    # to draw and nothing to lease.
    mo.stop(
        connection_error is not None,
        mo.callout(
            mo.md(
                f"""
                **No broker.** {connection_error}

                Start one beside the instrument with
                `miainwoodpecker-broker --publish .`, then Connect.
                """,
            ),
            kind="warn",
        ),
    )
    mo.md(f"Watching **{len(described)}** targets: `{'`, `'.join(described)}`")
    return broker, described


@app.cell
def _(mo):
    # The display tick, and the one control that decides what this costs
    # the network.
    #
    # RemoteBroker.snapshot's own docstring says a remote watcher should
    # prefer targets() for the chrome and latest() for the one source it
    # is showing, because snapshot ships every target's pixels on every
    # call. That advice is for a watcher showing *one* source. A grid
    # draws all of them, so the pixels are wanted either way and
    # snapshot is one round trip where the alternative is one plus one
    # per tile - and, more importantly, it reads state and frames under
    # the same lock, so a tile cannot show a rate from one pass beside
    # pixels from another.
    #
    # What that leaves is bandwidth, and here the interval *is* the
    # bandwidth. A tile is a preview - decimated, and the acquisition
    # path never sees it - so backing off to 5 s on a slow link costs
    # nothing that matters.
    refresh = mo.ui.refresh(
        label="Live update",
        options=["0.5s", "1s", "2s", "5s", "10s"],
        default_interval="1s",
    )
    refresh
    return (refresh,)


@app.cell
def _(SessionLog):
    # Created once, for the life of the kernel, and never rebuilt: this
    # is the session's record and a cell that reconstructed it would
    # erase the shift. Appended to from the acquisition worker, read
    # from here - SessionLog holds a lock for exactly that.
    log = SessionLog()
    return (log,)


@app.cell
def _(log, refresh):
    # This client's identity, learned from the broker rather than
    # guessed: the holder on a lease is filled in by the server from the
    # connection, precisely so a client cannot name itself whatever it
    # likes. Unknown until the first lease, which is why a tile claims
    # nothing about "leased by you" before then - and why this is on the
    # timer: the log object never changes identity, so without the tick
    # this cell would run once, before any lease existed, and the answer
    # would stay None for the life of the notebook.
    refresh
    holder = next(
        (entry.holder for entry in reversed(log.entries) if entry.holder),
        None,
    )
    return (holder,)


@app.cell
def _(channel_labels, is_image, mo, png_data_uri, tile_status):
    def blank(message):
        """Render a tile with no picture in it, saying why there is none."""
        return mo.Html(
            '<div style="width:100%;aspect-ratio:1;background:#000;'
            'border-radius:4px;display:flex;align-items:center;'
            'justify-content:center;color:#888;font-size:0.8em;'
            f'text-align:center;padding:0 1em;">{message}</div>',
        )

    def tile_card(tile):
        """Render one frame source: its picture, its title, its chrome."""
        if tile.frames and not is_image(tile.frames[0].data):
            # A camera in projected readout delivers a 1D spectrum, not
            # an image. Saying so beats drawing a one-pixel-high strip
            # and calling it a picture; a spectrum wants a plot, which
            # is a different tile than this one.
            picture = blank("1D readout - not an image")
            caption = ""
        elif tile.frames:
            # image-rendering: pixelated on purpose. The frame has
            # already been decimated to at most 512 px, and letting the
            # browser smooth what is left would draw interpolated pixels
            # over measured ones.
            picture = mo.Html(
                f'<img src="{png_data_uri(tile.frames[0].data)}" '
                'style="width:100%;aspect-ratio:1;object-fit:contain;'
                'background:#000;border-radius:4px;'
                'image-rendering:pixelated;" />',
            )
            names = channel_labels(tile)
            shown = names[0] if names else ""
            extra = (
                f" (+{len(tile.frames) - 1} more this pass)"
                if len(tile.frames) > 1
                else ""
            )
            caption = f"{shown}{extra}" if shown else ""
        else:
            # "No frame yet" is an ordinary state a tile renders every
            # time a loop starts, not an error - see InstrumentBroker.latest.
            picture = blank("no frame yet")
            caption = ""
        return mo.vstack(
            [
                mo.md(f"**{tile.label}** &nbsp; `{tile.name}`"),
                picture,
                mo.md(f"<small>{tile_status(tile)}</small>"),
                mo.md(f"<small>{caption}</small>") if caption else mo.md(""),
            ],
            gap=0.25,
        )

    def tile_grid(tiles, columns=3):
        """Lay tiles out in fixed rows, in the order describe() gave them."""
        rows = [
            mo.hstack(
                [tile_card(tile) for tile in tiles[start : start + columns]],
                widths="equal",
                align="start",
                gap=1,
            )
            for start in range(0, len(tiles), columns)
        ]
        return mo.vstack(rows, gap=1) if rows else mo.md(
            "*This instrument serves no scanner and no camera, so there is "
            "nothing to watch.*",
        )

    return (tile_grid,)


@app.cell
def _(broker, described, frame_tiles, holder, refresh, tile_grid):
    # Referenced so that the timer re-runs this cell; the value itself
    # is not used. This is the whole polling loop.
    refresh
    tiles = frame_tiles(described, broker.snapshot(), holder=holder)
    tile_grid(tiles)
    return (tiles,)


@app.cell
def _(mo):
    mo.md("## Acquire")
    return


@app.cell
def _(described, frame_sources, mo):
    # From describe(), NOT from the tiles - and the difference is a bug
    # rather than a nicety. The tiles are rebuilt on every poll, so a
    # control that depended on them would be rebuilt once a second and
    # would throw away whatever the operator had just typed into it.
    # describe() is static for the life of the instrument.
    sources = frame_sources(described)
    source = mo.ui.dropdown(
        label="Target",
        options=[description.name for description in sources],
        value=sources[0].name if sources else None,
    )
    frame_count = mo.ui.number(label="Frames", start=1, stop=1000, value=1)
    mo.hstack([source, frame_count], justify="start", gap=1)
    return frame_count, source, sources


@app.cell
def _(mo, source, sources):
    chosen = next(
        (description for description in sources if description.name == source.value),
        None,
    )
    # Every control below is built from describe(), never from a device
    # handle: a client in another process has no handle to read, and
    # this is the whole reason TargetDescription exists.
    detectors = mo.ui.array(
        [mo.ui.checkbox(value=index == 0, label=name)
         for index, name in enumerate(chosen.channel_names)]
        if chosen is not None
        else [],
    )
    factors = chosen.binning_values if chosen is not None else ()
    binning = mo.ui.dropdown(
        label="Binning",
        options={str(value): value for value in factors},
        value=str(factors[0]) if factors else None,
    )
    return binning, chosen, detectors


@app.cell
def _(binning, chosen, detectors, mo):
    scan_size = mo.ui.dropdown(
        label="Size (px)",
        options={str(size): size for size in (128, 256, 512, 1024, 2048)},
        value="512",
    )
    dwell_us = mo.ui.number(label="Dwell (us)", start=0.2, stop=1000.0, value=2.0)
    fov_nm = mo.ui.number(
        label="Field of view (nm)", start=1.0, stop=10000.0, value=100.0,
    )
    exposure_ms = mo.ui.number(
        label="Exposure (ms)", start=1.0, stop=10000.0, value=50.0,
    )
    controls = (
        mo.hstack([scan_size, dwell_us, fov_nm], justify="start", gap=1)
        if chosen is not None and chosen.kind == "scanner"
        else mo.hstack([exposure_ms, binning], justify="start", gap=1)
    )
    detector_row = (
        mo.vstack(
            [mo.md("**Detectors** - all of them read out of one pass"), detectors],
        )
        if chosen is not None and chosen.channel_names
        else mo.md("")
    )
    mo.vstack([controls, detector_row], gap=0.5)
    return dwell_us, exposure_ms, fov_nm, scan_size


@app.cell
def _(mo):
    session_form = mo.ui.text(
        label="Session directory (blank keeps frames in memory only)",
        full_width=True,
    ).form(submit_button_label="Use", bordered=False)
    note = mo.ui.text(label="Note for the next recording", full_width=True)
    mo.vstack([session_form, note], gap=0.5)
    return note, session_form


@app.cell
def _(Session, session_form):
    session = Session(session_form.value) if session_form.value else None
    return (session,)


@app.cell
def _(mo):
    acquire = mo.ui.run_button(label="Acquire")
    acquire
    return (acquire,)


@app.cell
def _(
    AcquisitionJob,
    CameraParameters,
    ScanParameters,
    acquire,
    binning,
    broker,
    camera_request,
    chosen,
    detectors,
    dwell_us,
    exposure_ms,
    fov_nm,
    frame_count,
    log,
    mo,
    note,
    scan_request,
    scan_size,
    session,
):
    # run_button's value is True only for the run its press triggered
    # and False on every other re-run, which is what makes this cell
    # safe to depend on a dozen controls: ticking a detector re-runs it
    # and stops here rather than starting an acquisition nobody asked
    # for.
    mo.stop(
        not acquire.value or chosen is None,
        mo.md("*Choose a target and press **Acquire**.*"),
    )
    if chosen.kind == "scanner":
        wanted = [index for index, box in enumerate(detectors.value) if box]
        request = scan_request(
            chosen.name,
            parameters=ScanParameters(
                height=scan_size.value,
                width=scan_size.value,
                pixel_time_us=dwell_us.value,
                fov_nm=fov_nm.value,
            ),
            # At least one, always: a scan with no detector enabled
            # reads nothing out, which is not a state an operator can
            # mean by unticking the last box.
            channels=wanted or [0],
            channel_names=[chosen.channel_names[index] for index in (wanted or [0])],
            count=int(frame_count.value),
        )
    else:
        # Settings apply to a single image only, and the split is the
        # acquisition layer's rather than this notebook's: camera_image
        # applies an exposure and puts the live one back, camera_series
        # deliberately touches no settings at all. See camera_request.
        request = camera_request(
            chosen.name,
            parameters=(
                CameraParameters(
                    exposure_ms=exposure_ms.value,
                    binning=int(binning.value or 1),
                )
                if int(frame_count.value) == 1
                else None
            ),
            count=int(frame_count.value),
        )
    # Started, not awaited. The lease is taken inside the job, on its
    # own thread, because taking one can block for as long as a scan
    # pass - see the module docstring.
    job = AcquisitionJob(
        broker, request, log, session=session, note=note.value or None,
    )
    job.start()
    mo.md(f"Started **{request.label}** - {request.reason}")
    return (job,)


@app.cell
def _(job, mo, refresh):
    refresh
    if job.is_running:
        status = mo.md(f"Acquiring - **{job.frames_acquired}** frames so far.")
    elif job.error is not None:
        # Refusals are shown as sentences, not tracebacks: the broker's
        # own message names who holds the instrument and what for.
        status = mo.callout(mo.md(f"**Refused.** {job.error}"), kind="danger")
    else:
        status = mo.callout(
            mo.md("**Done.** See the session log below."), kind="success",
        )
    status
    return


@app.cell
def _(mo):
    mo.md(
        """
        ## Session log

        Append-only. Every attempt is here, refusals included - "the
        scanner was leased by the viewer" is part of what happened, and
        a log that dropped it would leave a gap somebody later reads as
        a quiet minute.
        """,
    )
    return


@app.cell
def _(highlights, log, mo, refresh):
    refresh

    def log_row(entry):
        """Render one acquisition: thumbnail on the left, provenance on the right."""
        if entry.error is not None:
            body = mo.callout(mo.md(f"**Refused.** {entry.error}"), kind="danger")
            return mo.vstack(
                [
                    mo.md(
                        f"**{entry.index}. {entry.label}** - "
                        f"{entry.started_at:%H:%M:%S} - {entry.reason}",
                    ),
                    body,
                ],
                gap=0.25,
            )
        where = entry.recording_path or "in memory only (no session attached)"
        facts = "\n".join(
            f"- `{key}`: {value}" for key, value in highlights(entry).items()
        )
        return mo.hstack(
            [
                mo.Html(
                    f'<img src="{entry.thumbnail}" style="width:128px;'
                    'background:#000;border-radius:4px;'
                    'image-rendering:pixelated;" />',
                ),
                mo.md(
                    f"**{entry.index}. {entry.label}** - "
                    f"{entry.started_at:%H:%M:%S} - "
                    f"{entry.frame_count} frames of {entry.shape} "
                    f"{entry.dtype} in {entry.duration_s:.1f} s\n\n"
                    f"held as `{entry.holder}` for *{entry.reason}*\n\n"
                    f"written to `{where}`\n\n{facts}",
                ),
            ],
            widths=[1, 5],
            align="start",
            gap=1,
        )

    entries = log.entries
    mo.vstack(
        [log_row(entry) for entry in reversed(entries)],
        gap=1,
    ) if entries else mo.md("*Nothing acquired yet.*")
    return


if __name__ == "__main__":
    app.run()
