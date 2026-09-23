#!/usr/bin/env python3
import runpy
import sys


DEVICE = "zzw"


def main() -> None:
    if "--device" in sys.argv:
        idx = sys.argv.index("--device")
        if idx + 1 >= len(sys.argv) or sys.argv[idx + 1] != DEVICE:
            raise SystemExit("zzw image only accepts --device zzw")
    else:
        sys.argv.extend(["--device", DEVICE])
    sys.path.insert(0, "/usr/local/lib/radio_protocol")
    runpy.run_path("/usr/local/lib/radio_protocol/radio_param_service.py", run_name="__main__")


if __name__ == "__main__":
    main()
