import argparse
import collections
import json
import statistics
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("capture", type=Path)
    args = parser.parse_args()
    groups = collections.defaultdict(list)
    requests = []
    with args.capture.open(encoding="utf-8") as source:
        for line in source:
            record = json.loads(line)
            if record.get("type") == "report":
                payload = bytes.fromhex(record["hex"])
                identity = (record.get("vendor_id"), record.get("product_id"), record["report_id"], len(payload))
                groups[identity].append((record["host_ms"], payload))
            elif record.get("type") == "request":
                requests.append(record)
    result = {"groups": [], "requests": requests}
    for identity, reports in groups.items():
        intervals = [second[0] - first[0] for first, second in zip(reports, reports[1:])]
        varying = []
        for offset in range(identity[3]):
            values = {payload[offset] for _, payload in reports}
            if len(values) > 1:
                varying.append({"offset": offset, "distinct": len(values), "min": min(values), "max": max(values)})
        result["groups"].append({"vendor_id": identity[0], "product_id": identity[1], "report_id": identity[2], "payload_bytes": identity[3], "reports": len(reports), "first_ms": reports[0][0], "last_ms": reports[-1][0], "median_interval_ms": statistics.median(intervals) if intervals else None, "varying_bytes": varying, "unique_payloads": len({payload for _, payload in reports})})
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
