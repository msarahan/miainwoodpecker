"""
Napari dock widget for the live "look at the sample, adjust settings" loop.

One :class:`~miainwoodpecker.acquisition.live.LiveAcquisition` per source
(scan, camera) grabs frames on worker threads; a single QTimer on the GUI
thread polls their latest frames at display rate and pushes them into
napari image layers. Acquisition rate and display rate are fully
decoupled — a slow display skips frames, and a fast source never floods
the UI with events.

Thread-safety contract: Qt widgets are only touched from the GUI thread.
Scan settings are written to a plain tuple attribute by the GUI thread
whenever a control changes, and the worker thread only reads that
attribute — no Qt access from workers.

Importing this module requires the ``viewer`` optional dependency group.
The camera group's "Analyze in HyperSpy", "Sum in LiberTEM", and "Fit
central disk (py4DSTEM)" buttons additionally need the ``analysis``,
``libertem``, and ``py4dstem`` groups respectively (migration plan,
Phase 4); all three libraries are imported lazily so this module still
imports and the buttons still render without them, only reporting the
missing extra in the status label if clicked.
"""

from __future__ import annotations

import tempfile
import typing
from pathlib import Path

from qtpy import QtCore, QtWidgets

from miainwoodpecker.acquisition.live import LiveAcquisition
from miainwoodpecker.acquisition.sequence import camera_series
from miainwoodpecker.devices.interface import ScanParameters
from miainwoodpecker.storage.nexus import write_frames

if typing.TYPE_CHECKING:
    import napari

    from miainwoodpecker.devices.interface import Camera, Frame, Scanner

_DEFAULT_DISPLAY_INTERVAL_MS = 33
_SCAN_SIZES = (128, 256, 512)
_DEFAULT_SCAN_SIZE_INDEX = 1  # 256
_DEFAULT_DWELL_US = 1.0
_DEFAULT_FOV_NM = 15.0
_ANALYSIS_BURST_FRAME_COUNT = 5


class LiveInstrumentWidget(QtWidgets.QWidget):
    """
    Dock widget with live scan (and optionally camera) view controls.

    Parameters
    ----------
    viewer : napari.Viewer
        The napari viewer whose layers display the live frames.
    scanner : Scanner
        The scan device to drive.
    camera : Camera | None
        An optional camera to offer a live view for (e.g. Ronchigram).
    display_interval_ms : int
        How often the display polls for new frames.
    parent : QtWidgets.QWidget | None
        Optional Qt parent widget.
    """

    def __init__(
        self,
        viewer: napari.Viewer,
        scanner: Scanner,
        *,
        camera: Camera | None = None,
        display_interval_ms: int = _DEFAULT_DISPLAY_INTERVAL_MS,
        parent: QtWidgets.QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._viewer = viewer
        self._scanner = scanner
        self._camera = camera
        self._scan_loop: LiveAcquisition | None = None
        self._camera_loop: LiveAcquisition | None = None
        self._scan_request: tuple[ScanParameters, int, str] = (
            ScanParameters(
                height=_SCAN_SIZES[_DEFAULT_SCAN_SIZE_INDEX],
                width=_SCAN_SIZES[_DEFAULT_SCAN_SIZE_INDEX],
                pixel_time_us=_DEFAULT_DWELL_US,
                fov_nm=_DEFAULT_FOV_NM,
            ),
            0,
            scanner.channel_names[0],
        )
        self._build_ui()
        self._on_scan_settings_changed()
        self._timer = QtCore.QTimer(self)
        self._timer.setInterval(display_interval_ms)
        self._timer.timeout.connect(self.refresh_display)

    def _build_ui(self) -> None:
        layout = QtWidgets.QVBoxLayout(self)
        layout.addWidget(self._build_scan_group())
        if self._camera is not None:
            layout.addWidget(self._build_camera_group())
        layout.addStretch(1)

    def _build_scan_group(self) -> QtWidgets.QGroupBox:
        scan_group = QtWidgets.QGroupBox("Scan", self)
        scan_form = QtWidgets.QFormLayout(scan_group)
        self._channel_combo = QtWidgets.QComboBox(scan_group)
        self._channel_combo.addItems(list(self._scanner.channel_names))
        scan_form.addRow("Channel", self._channel_combo)
        self._size_combo = QtWidgets.QComboBox(scan_group)
        self._size_combo.addItems([str(size) for size in _SCAN_SIZES])
        self._size_combo.setCurrentIndex(_DEFAULT_SCAN_SIZE_INDEX)
        scan_form.addRow("Size (px)", self._size_combo)
        self._dwell_spin = QtWidgets.QDoubleSpinBox(scan_group)
        self._dwell_spin.setRange(0.1, 1000.0)
        self._dwell_spin.setValue(_DEFAULT_DWELL_US)
        self._dwell_spin.setSuffix(" µs")
        scan_form.addRow("Dwell", self._dwell_spin)
        self._fov_spin = QtWidgets.QDoubleSpinBox(scan_group)
        self._fov_spin.setRange(0.1, 100000.0)
        self._fov_spin.setValue(_DEFAULT_FOV_NM)
        self._fov_spin.setSuffix(" nm")
        scan_form.addRow("FOV", self._fov_spin)
        self._scan_button = QtWidgets.QPushButton("Start scan", scan_group)
        scan_form.addRow(self._scan_button)
        self._scan_status = QtWidgets.QLabel("stopped", scan_group)
        scan_form.addRow("Status", self._scan_status)

        self._channel_combo.currentIndexChanged.connect(self._on_scan_settings_changed)
        self._size_combo.currentIndexChanged.connect(self._on_scan_settings_changed)
        self._dwell_spin.valueChanged.connect(self._on_scan_settings_changed)
        self._fov_spin.valueChanged.connect(self._on_scan_settings_changed)
        self._scan_button.clicked.connect(self._toggle_scan)
        return scan_group

    def _build_camera_group(self) -> QtWidgets.QGroupBox:
        camera_group = QtWidgets.QGroupBox("Camera", self)
        camera_form = QtWidgets.QFormLayout(camera_group)
        self._camera_button = QtWidgets.QPushButton("Start camera", camera_group)
        camera_form.addRow(self._camera_button)
        self._camera_status = QtWidgets.QLabel("stopped", camera_group)
        camera_form.addRow("Status", self._camera_status)
        self._analyze_button = QtWidgets.QPushButton(
            "Analyze in HyperSpy", camera_group
        )
        camera_form.addRow(self._analyze_button)
        self._analyze_status = QtWidgets.QLabel("", camera_group)
        camera_form.addRow("Analysis", self._analyze_status)
        self._libertem_button = QtWidgets.QPushButton("Sum in LiberTEM", camera_group)
        camera_form.addRow(self._libertem_button)
        self._libertem_status = QtWidgets.QLabel("", camera_group)
        camera_form.addRow("LiberTEM", self._libertem_status)
        self._py4dstem_button = QtWidgets.QPushButton(
            "Fit central disk (py4DSTEM)", camera_group
        )
        camera_form.addRow(self._py4dstem_button)
        self._py4dstem_status = QtWidgets.QLabel("", camera_group)
        camera_form.addRow("py4DSTEM", self._py4dstem_status)

        self._camera_button.clicked.connect(self._toggle_camera)
        self._analyze_button.clicked.connect(self._analyze_camera_in_hyperspy)
        self._libertem_button.clicked.connect(self._analyze_camera_in_libertem)
        self._py4dstem_button.clicked.connect(self._fit_central_disk_in_py4dstem)
        return camera_group

    def _on_scan_settings_changed(self) -> None:
        size = int(self._size_combo.currentText())
        channel_index = self._channel_combo.currentIndex()
        self._scan_request = (
            ScanParameters(
                height=size,
                width=size,
                pixel_time_us=self._dwell_spin.value(),
                fov_nm=self._fov_spin.value(),
            ),
            channel_index,
            self._channel_combo.currentText(),
        )

    def _grab_scan(self) -> Frame:
        # Runs on the worker thread: reads the request tuple, never Qt state.
        parameters, channel_index, _ = self._scan_request
        return self._scanner.scan_frame(parameters, channel_index)

    def _toggle_scan(self) -> None:
        if self._scan_loop is not None and self._scan_loop.is_running:
            self.stop_scan()
        else:
            self.start_scan()

    def _toggle_camera(self) -> None:
        if self._camera_loop is not None and self._camera_loop.is_running:
            self.stop_camera()
        else:
            self.start_camera()

    def start_scan(self) -> None:
        """Start the live scan loop and the display timer."""
        if self._scan_loop is not None and self._scan_loop.is_running:
            return
        self._scan_loop = LiveAcquisition(self._grab_scan)
        self._scan_loop.start()
        self._scan_button.setText("Stop scan")
        self._scan_status.setText("running")
        self._timer.start()

    def stop_scan(self) -> None:
        """Stop the live scan loop."""
        if self._scan_loop is not None:
            self._scan_loop.stop()
        self._scan_button.setText("Start scan")
        self._scan_status.setText("stopped")
        self._maybe_stop_timer()

    def start_camera(self) -> None:
        """Start the camera and its live loop and the display timer."""
        if self._camera is None:
            return
        if self._camera_loop is not None and self._camera_loop.is_running:
            return
        self._camera.start()
        self._camera_loop = LiveAcquisition(self._camera.acquire_frame)
        self._camera_loop.start()
        self._camera_button.setText("Stop camera")
        self._camera_status.setText("running")
        self._timer.start()

    def stop_camera(self) -> None:
        """Stop the camera's live loop and pause the camera."""
        if self._camera_loop is not None:
            self._camera_loop.stop()
        if self._camera is not None:
            self._camera.stop()
        if self._camera is not None:
            self._camera_button.setText("Start camera")
            self._camera_status.setText("stopped")
        self._maybe_stop_timer()

    def _analyze_camera_in_hyperspy(self) -> None:
        """
        Round-trip a short camera burst through the HyperSpy adapter.

        Demonstrates the Phase 4 analysis-integration path end to end:
        stop the live camera loop (so this button and the loop never
        drive the same device at once), record a short burst straight to
        a temporary NeXus file with
        :func:`~miainwoodpecker.storage.nexus.write_frames`, read it back
        as a HyperSpy signal with
        :func:`~miainwoodpecker.analysis.hyperspy_bridge.load_as_hyperspy_signal`,
        run one real HyperSpy operation
        (:meth:`hyperspy.signals.Signal2D.mean` over the frame axis), and
        push the result into napari as a new image layer. Requires the
        ``analysis`` optional dependency group; reports that in the
        status label rather than crashing the widget if it is missing.
        """
        if self._camera is None:
            return
        try:
            from miainwoodpecker.analysis.hyperspy_bridge import (  # noqa: PLC0415
                load_as_hyperspy_signal,
            )
        except ImportError:
            self._analyze_status.setText("install the 'analysis' extra")
            return

        if self._camera_loop is not None and self._camera_loop.is_running:
            self.stop_camera()

        self._analyze_status.setText("recording...")
        try:
            frames = list(camera_series(self._camera, _ANALYSIS_BURST_FRAME_COUNT))
            with tempfile.TemporaryDirectory() as tmp_dir:
                burst_path = Path(tmp_dir) / "hyperspy_analysis_burst.nxs"
                write_frames(burst_path, frames, title="hyperspy analysis burst")
                signal = load_as_hyperspy_signal(burst_path)
                projection = signal.mean(axis=signal.axes_manager.navigation_axes[0])
        except Exception as exc:  # noqa: BLE001 - surfaced in the status label
            self._analyze_status.setText(f"error: {exc}")
            return

        self._viewer.add_image(
            projection.data,
            name="HyperSpy mean projection (Camera)",
            colormap="viridis",
        )
        self._analyze_status.setText(f"done - mean of {len(frames)} frames")

    def _analyze_camera_in_libertem(self) -> None:
        """
        Round-trip a short camera burst through the LiberTEM adapter.

        The second half of the Phase 4 analysis-integration path: stop the
        live camera loop if running, record a short burst to a temporary
        NeXus file with
        :func:`~miainwoodpecker.storage.nexus.write_frames`, read it back
        as a LiberTEM ``DataSet`` with
        :func:`~miainwoodpecker.analysis.libertem_bridge.load_as_libertem_dataset`,
        run one real LiberTEM UDF (``libertem.udf.sum.SumUDF``, summing
        across the frame/navigation axis) with an inline ``Context``, and
        push the result into napari as a new image layer. Requires the
        ``libertem`` optional dependency group; reports that in the status
        label rather than crashing the widget if it is missing.
        """
        if self._camera is None:
            return
        try:
            from libertem.api import Context  # noqa: PLC0415
            from libertem.udf.sum import SumUDF  # noqa: PLC0415

            from miainwoodpecker.analysis.libertem_bridge import (  # noqa: PLC0415
                load_as_libertem_dataset,
            )
        except ImportError:
            self._libertem_status.setText("install the 'libertem' extra")
            return

        if self._camera_loop is not None and self._camera_loop.is_running:
            self.stop_camera()

        self._libertem_status.setText("recording...")
        try:
            frames = list(camera_series(self._camera, _ANALYSIS_BURST_FRAME_COUNT))
            with tempfile.TemporaryDirectory() as tmp_dir:
                burst_path = Path(tmp_dir) / "libertem_analysis_burst.nxs"
                write_frames(burst_path, frames, title="libertem analysis burst")
                # Inline executor: this is a single UDF run over one small,
                # already-in-memory burst, not the large-dataset workload
                # LiberTEM's default dask executor is built for - spinning
                # up a local cluster per button click would be pure
                # overhead here.
                with Context.make_with("inline") as ctx:
                    dataset = load_as_libertem_dataset(ctx, burst_path)
                    result = ctx.run_udf(dataset=dataset, udf=SumUDF())
                    sum_projection = result["intensity"].data
        except Exception as exc:  # noqa: BLE001 - surfaced in the status label
            self._libertem_status.setText(f"error: {exc}")
            return

        self._viewer.add_image(
            sum_projection,
            name="LiberTEM sum projection (Camera)",
            colormap="viridis",
        )
        self._libertem_status.setText(f"done - sum of {len(frames)} frames")

    def _fit_central_disk_in_py4dstem(self) -> None:
        """
        Round-trip one real camera frame through the py4DSTEM adapter.

        Demonstrates the py4DSTEM follow-up to Phase 4 (migration plan,
        §5) end to end: stop the live camera loop if running, acquire one
        real frame via :func:`~miainwoodpecker.acquisition.sequence.camera_series`,
        write it to a temporary NeXus file with
        :func:`~miainwoodpecker.storage.nexus.write_frames`, read it back
        as a py4DSTEM ``DiffractionSlice`` with
        :func:`~miainwoodpecker.analysis.py4dstem_bridge.load_as_diffraction_slice`,
        run one real py4DSTEM operation on that single diffraction pattern
        (``py4DSTEM.process.calibration.get_probe_size``, the same
        central-disk fit py4DSTEM runs per-pattern inside a full
        datacube), and push both the analyzed frame and a napari ``Shapes``
        ellipse at the fitted disk into the viewer. Only a single frame is
        used, not a scan-position-indexed cube - see
        :mod:`miainwoodpecker.analysis.py4dstem_bridge` for why that cube
        isn't available yet. Requires the ``py4dstem`` optional dependency
        group; reports that in the status label rather than crashing the
        widget if it is missing.
        """
        if self._camera is None:
            return
        try:
            from py4DSTEM.process.calibration import get_probe_size  # noqa: PLC0415

            from miainwoodpecker.analysis.py4dstem_bridge import (  # noqa: PLC0415
                load_as_diffraction_slice,
            )
        except ImportError:
            self._py4dstem_status.setText("install the 'py4dstem' extra")
            return

        if self._camera_loop is not None and self._camera_loop.is_running:
            self.stop_camera()

        self._py4dstem_status.setText("acquiring...")
        try:
            (frame,) = camera_series(self._camera, 1)
            with tempfile.TemporaryDirectory() as tmp_dir:
                frame_path = Path(tmp_dir) / "py4dstem_analysis_frame.nxs"
                write_frames(frame_path, [frame], title="py4DSTEM analysis frame")
                diffraction_slice = load_as_diffraction_slice(frame_path)
                radius, x0, y0 = get_probe_size(diffraction_slice.data)
        except Exception as exc:  # noqa: BLE001 - surfaced in the status label
            self._py4dstem_status.setText(f"error: {exc}")
            return

        self._viewer.add_image(
            diffraction_slice.data,
            name="py4DSTEM disk fit (Camera)",
            colormap="gray",
        )
        self._viewer.add_shapes(
            [
                [y0 - radius, x0 - radius],
                [y0 - radius, x0 + radius],
                [y0 + radius, x0 + radius],
                [y0 + radius, x0 - radius],
            ],
            shape_type="ellipse",
            name="py4DSTEM disk fit",
            edge_color="red",
            face_color="transparent",
        )
        self._py4dstem_status.setText(
            f"done - r={radius:.1f}px center=({x0:.1f}, {y0:.1f})"
        )

    def _maybe_stop_timer(self) -> None:
        scan_running = self._scan_loop is not None and self._scan_loop.is_running
        camera_running = (
            self._camera_loop is not None and self._camera_loop.is_running
        )
        if not scan_running and not camera_running:
            self._timer.stop()

    def refresh_display(self) -> None:
        """Push the newest frames into napari layers; called by the display timer."""
        if self._scan_loop is not None:
            self._refresh_source(
                self._scan_loop,
                layer_name=f"Scan ({self._scan_request[2]})",
                status_label=self._scan_status,
                autocontrast_every_frame=True,
            )
        if self._camera_loop is not None:
            self._refresh_source(
                self._camera_loop,
                layer_name="Camera",
                status_label=self._camera_status,
                autocontrast_every_frame=False,
            )

    def _refresh_source(
        self,
        loop: LiveAcquisition,
        *,
        layer_name: str,
        status_label: QtWidgets.QLabel,
        autocontrast_every_frame: bool,
    ) -> None:
        if loop.error is not None:
            status_label.setText(f"error: {loop.error}")
            return
        frame = loop.latest()
        if frame is None:
            return
        if layer_name in self._viewer.layers:
            layer = self._viewer.layers[layer_name]
            layer.data = frame.data
            if autocontrast_every_frame:
                low = float(frame.data.min())
                high = float(frame.data.max())
                if high > low:
                    layer.contrast_limits = (low, high)
        else:
            self._viewer.add_image(frame.data, name=layer_name, colormap="gray")
        if loop.is_running:
            status_label.setText(f"running - {loop.stats.fps:.1f} fps")

    def shutdown(self) -> None:
        """Stop all loops, the camera, and the display timer."""
        self._timer.stop()
        self.stop_scan()
        self.stop_camera()

    def closeEvent(self, event: typing.Any) -> None:  # noqa: N802, ANN401 - Qt override
        """Shut down cleanly when the widget is closed."""
        self.shutdown()
        super().closeEvent(event)
