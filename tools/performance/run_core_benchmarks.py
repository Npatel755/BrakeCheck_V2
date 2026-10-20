"""Release-mode algorithm benchmarks against a committed baseline; never uses the reserved dataset test split."""
from pathlib import Path
import argparse
import json
import math
import subprocess
import sys

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--baseline", default="c3e1095")
    parser.add_argument("--distribution", default="Ubuntu-24.04")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[2]
    output = root / "test-results/performance-n61"
    output.mkdir(parents=True, exist_ok=True)
    names = subprocess.check_output(["git", "ls-tree", "-r", "--name-only", args.baseline, "ios/Core"], cwd=root, text=True).splitlines()
    for name in names:
        if "/Sources/" in name and name.endswith(".swift") or name == "ios/Core/Package.swift":
            target = output / "baseline" / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(subprocess.check_output(["git", "show", f"{args.baseline}:{name}"], cwd=root))
    source = (root / "tools/performance/Benchmark.swift").read_text(encoding="utf-8")
    records = {}
    for variant in ["baseline", "optimized"]:
        harness = output / ("harness-" + variant)
        sources = harness / "Sources/BrakePerformanceBench"
        sources.mkdir(parents=True, exist_ok=True)
        (sources / "main.swift").write_text(source, encoding="utf-8", newline="\n")
        core_path = "../baseline/ios/Core" if variant == "baseline" else "../../../ios/Core"
        settings = '[.define("OPTIMIZED")]' if variant == "optimized" else '[]'
        package = f'''// swift-tools-version: 6.0
import PackageDescription
let package = Package(name: "BrakePerformanceBench",
    dependencies: [.package(path: "{core_path}")],
    targets: [.executableTarget(name: "BrakePerformanceBench",
        dependencies: [.product(name: "BrakeCheckCore", package: "Core")],
        swiftSettings: {settings})],
    swiftLanguageModes: [.v5])
'''
        (harness / "Package.swift").write_text(package, encoding="utf-8", newline="\n")
        log = output / (variant + ".log")
        # Pass the entire shell command as one WSL argument; no variable interpolation.
        posix = "/mnt/" + harness.drive[0].lower() + harness.as_posix()[2:]
        command = f"cd '{posix}' && swift run -c release"
        with log.open("w", encoding="utf-8") as stream:
            result = subprocess.run(["wsl", "-d", args.distribution, "bash", "-lc", command],
                stdout=stream, stderr=subprocess.STDOUT)
        if result.returncode:
            print(log.read_text(encoding="utf-8")[-12000:])
            raise SystemExit(result.returncode)
        record = next(json.loads(line) for line in log.read_text(encoding="utf-8").splitlines() if line.startswith('{"benchmarks"'))
        records[variant] = record["benchmarks"]
        print(variant, json.dumps(record), flush=True)
    comparisons = []
    for before, after in zip(records["baseline"], records["optimized"]):
        assert before["name"] == after["name"]
        assert math.isclose(before["checksum"], after["checksum"], rel_tol=1e-8, abs_tol=1e-6), (before, after)
        comparisons.append({"name": before["name"], "baseline_ms": before["medianMilliseconds"],
            "optimized_ms": after["medianMilliseconds"],
            "speedup": before["medianMilliseconds"] / max(1e-9, after["medianMilliseconds"]),
            "baseline_preparation_ms": before["preparationMedianMilliseconds"],
            "optimized_preparation_ms": after["preparationMedianMilliseconds"],
            "checksum_matched": True})
    report = {"baseline": args.baseline, "mode": "Swift release on WSL x86_64, synthetic inputs",
        "repetitions": 5, "comparisons": comparisons,
        "limits": "Algorithm timing, not iPhone FPS or physical-device validation. Initialization costs shown separately."}
    (output / "comparison.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2), flush=True)
if __name__ == "__main__":
    main()
