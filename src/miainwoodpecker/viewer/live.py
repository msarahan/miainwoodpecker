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
The camera group's "Analyze in HyperSpy" button additionally needs the
``analysis`` group (migration plan, Phase 4); it is imported lazily so
this module still imports and the button still renders without it, only
reporting the missing extra in the status label if clicked.
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
        layout.addWidget(scan_group)

        self._channel_combo.currentIndexChanged.connect(self._on_scan_settings_changed)
        self._size_combo.currentIndexChanged.connect(self._on_scan_settings_changed)
        self._dwell_spin.valueChanged.connect(self._on_scan_settings_changed)
        self._fov_spin.valueChanged.connect(self._on_scan_settings_changed)
        self._scan_button.clicked.connect(self._toggle_scan)

        if self._camera is not None:
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
            layout.addWidget(camera_group)
            self._camera_button.clicked.connect(self._toggle_camera)
            self._analyze_button.clicked.connect(self._analyze_camera_in_hyperspy)

        layout.addStretch(1)

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
