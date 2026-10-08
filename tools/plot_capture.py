import argparse
import json
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description="Plot raw HID fields against receipt time")
    parser.add_argument("capture", type=Path)
    parser.add_argument("--out", type=Path, default=Path("capture-plot.html"))
    args = parser.parse_args()
    reports = []
    includes_id = False
    with args.capture.open(encoding="utf-8") as source:
        for line in source:
            record = json.loads(line)
            if record.get("type") == "metadata":
                includes_id = not record.get("payload_excludes_report_id", True)
            elif record.get("type") == "report":
                payload = list(bytes.fromhex(record["hex"]))
                if includes_id:
                    payload = payload[1:]
                reports.append([record.get("host_ms", record.get("host_ns", 0) / 1_000_000), record["report_id"], payload])
    if not reports:
        raise SystemExit("No reports in capture")
    template = Path(__file__).with_name("plot_template.html").read_text(encoding="utf-8")
    args.out.write_text(template.replace("__CAPTURE_DATA__", json.dumps(reports)), encoding="utf-8")
    print(args.out.resolve())


if __name__ == "__main__":
    main()
