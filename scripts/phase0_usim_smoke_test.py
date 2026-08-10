"""
Confirm that Nion's STEM device layer can be driven headlessly.

Phase 0 groundwork check (see docs/migration-plan.md), outside Swift's
own process/UI.

This talks directly to the `nion.device_kit` camera/scan device objects
that `nionswift-usim` builds (the same objects Swift's own hardware source
panels drive), with no `nion.swift.Application`/`DocumentController`
involved. That's the low-risk reuse path described in migration-plan.md
section 2: this device layer is kept, not rewritten.

Run with: uv run --extra device python scripts/phase0_usim_smoke_test.py
"""

from __future__ import annotations

import time

from nion.device_kit import ScanDevice
from nion.usim_device import DeviceConfiguration


def main() -> None:
    """Drive a simulated STEM camera and scanner headlessly and report frame shapes."""
    print("building simulated instrument + camera + scan devices...", flush=True)
    configuration = DeviceConfiguration.AcquisitionContextConfiguration(
        set_configuration_location=False,
    )
    camera_device = configuration.ronchigram_camera_device
    scan_device = configuration.scan_module.device

    try:
        print("starting camera live acquisition...", flush=True)
        camera_device.start_live()
        try:
            t0 = time.time()
            data_element = camera_device.acquire_image()
            elapsed = time.time() - t0
            data = data_element["data"]
            print(
                f"camera frame: shape={data.shape} dtype={data.dtype} ({elapsed:.2f}s)",
                flush=True,
            )
        finally:
            camera_device.stop_live()

        frame_parameters = ScanDevice.ScanFrameParameters(
            pixel_size=(64, 64),
            pixel_time_us=1,
            fov_nm=configuration.instrument.stage_size_nm * 0.1,
        )
        t0 = time.time()
        scan_data = scan_device.get_scan_data(frame_parameters, 0)
        elapsed = time.time() - t0
        shape, dtype = scan_data.shape, scan_data.dtype
        print(f"scan frame: shape={shape} dtype={dtype} ({elapsed:.2f}s)", flush=True)
    finally:
        # Both cameras built by AcquisitionContextConfiguration run their own
        # acquisition thread from construction time; close both or the
        # process will hang on exit waiting for the un-closed one to join.
        camera_device.close()
        configuration.eels_camera_device.close()
        scan_device.close()

    print("OK: drove nionswift-usim camera + scan devices headlessly", flush=True)


if __name__ == "__main__":
    main()
